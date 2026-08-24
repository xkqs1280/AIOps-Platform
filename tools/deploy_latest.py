# -*- coding: utf-8 -*-
"""部署 AIOps v4.0 到测试服务器。

同步 backend/app（保留 .env）与 frontend/dist，干净重启 uvicorn，验证。

连接凭据通过环境变量提供（勿硬编码进仓库）：
  AIOPS_DEPLOY_HOST        服务器 IP（默认 <server-ip>）
  AIOPS_DEPLOY_USER        SSH 用户名（默认 admin）
  AIOPS_DEPLOY_PASSWORD    SSH 密码（必填）
  AIOPS_DEPLOY_ADMIN_PASSWORD  平台管理员密码（验证登录用，可选）
"""
import os
import sys
import time

import paramiko

HOST = os.environ.get("AIOPS_DEPLOY_HOST", "<server-ip>")
USER = os.environ.get("AIOPS_DEPLOY_USER", "admin")
PWD = os.environ.get("AIOPS_DEPLOY_PASSWORD", "")
BASE = "/opt/aiops-platform"
LOCAL = os.environ.get("AIOPS_LOCAL_DIR", r".")


if not PWD:
    print("未设置 AIOPS_DEPLOY_PASSWORD 环境变量，拒绝部署。")
    sys.exit(1)
ADMIN_PASSWORD = os.environ.get("AIOPS_DEPLOY_ADMIN_PASSWORD", "<admin-password>")


def main():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, username=USER, password=PWD, timeout=15)
    sftp = client.open_sftp()

    def run(cmd, check=False):
        stdin, stdout, stderr = client.exec_command(cmd)
        out = stdout.read().decode()
        err = stderr.read().decode()
        rc = stdout.channel.recv_exit_status()
        if check and rc != 0:
            print(f"[FAIL] {cmd}\n  rc={rc}\n  {out}\n  {err}")
            sys.exit(1)
        return out, err, rc

    # 1. 备份
    ts = time.strftime("%Y%m%d%H%M%S")
    bk = f"{BASE}/_backup_v35_{ts}"
    print(f"[1/5] 备份到 {bk}")
    run(f"mkdir -p {bk}/app {bk}/dist")

    # 2. 同步后端 backend/app（排除 __pycache__，保留 .env）
    print("[2/5] 同步 backend/app")
    run(f"cp -r {BASE}/backend/app {bk}/app/")
    backend_app = os.path.join(LOCAL, "backend", "app")
    for root, dirs, files in os.walk(backend_app):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = os.path.relpath(root, backend_app)
        remote_dir = f"{BASE}/backend/app" if rel == "." else f"{BASE}/backend/app/{rel.replace(os.sep, '/')}"
        try:
            sftp.mkdir(remote_dir)
        except IOError:
            pass
        for f in files:
            local = os.path.join(root, f)
            remote = f"{remote_dir}/{f}"
            sftp.put(local, remote)
    # 清理服务器 __pycache__（避免旧字节码）
    run(f"find {BASE}/backend/app -name '__pycache__' -type d -exec rm -rf {{}} + 2>/dev/null || true")
    n = sum(len(fs) for _, _, fs in os.walk(backend_app) if not any(d == "__pycache__" for d in fs))
    print(f"  后端文件已同步")

    # 3. 同步前端 dist（先删旧 assets）
    print("[3/5] 同步 frontend/dist")
    run(f"cp -r {BASE}/frontend/dist {bk}/dist/")
    run(f"rm -rf {BASE}/frontend/dist/assets")
    frontend_dist = os.path.join(LOCAL, "frontend", "dist")
    for root, dirs, files in os.walk(frontend_dist):
        rel = os.path.relpath(root, frontend_dist).replace("\\", "/")
        remote_dir = f"{BASE}/frontend/dist" if rel == "." else f"{BASE}/frontend/dist/{rel}"
        try:
            sftp.mkdir(remote_dir)
        except IOError:
            pass
        for f in files:
            sftp.put(os.path.join(root, f), f"{remote_dir}/{f}")
    print("  前端 dist 已同步")

    # 4. 干净重启 uvicorn
    print("[4/5] 重启 uvicorn")
    # 用 -9 强制杀：SSE/WS 长连接会让优雅关闭一直等待连接关闭、端口不释放，
    # 导致新进程 bind 失败（health 一直 000）。先确认旧进程退出再启动。
    run("pkill -9 -f 'uvicorn app.main:app' || true")
    for _ in range(6):
        time.sleep(1)
        out, _, _ = run("pgrep -f 'uvicorn app.main:app' || true")
        if not out.strip():
            break
    time.sleep(1)
    run(f"cd {BASE}/backend && nohup .venv/bin/python3 .venv/bin/uvicorn app.main:app "
        f"--host 0.0.0.0 --port 8000 > {BASE}/backend/uvicorn.log 2>&1 &")
    ok = False
    for _ in range(30):
        time.sleep(1)
        out, _, _ = run("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/health")
        if out.strip() == "200":
            ok = True
            break
    if not ok:
        print("[FAIL] uvicorn 未就绪")
        print(run(f"tail -30 {BASE}/backend/uvicorn.log")[0])
        sys.exit(1)
    print("  uvicorn 已启动")

    # 5. 验证
    print("[5/5] 验证")
    import json as _json
    _login_body = _json.dumps({"username": "admin", "password": ADMIN_PASSWORD}, separators=(",", ":"))
    checks = [
        ("/health", "curl -s http://127.0.0.1:8000/health"),
        ("版本", "grep -o 'version.: .4.0.0.' /home/admin1/aiops-platform/backend/app/main.py | head -1"),
        ("根路径(无cookie)", "curl -s -o /dev/null -w '%{http_code} %{redirect_url}' http://127.0.0.1:8000/"),
        ("登录", f"curl -s -c /tmp/latest_cookies.txt -X POST http://127.0.0.1:8000/api/v1/auth/login -H 'Content-Type: application/json' -d '{_login_body}' -o /dev/null -w '%{{http_code}}'"),
        ("devices", "curl -s -b /tmp/latest_cookies.txt -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/v1/devices"),
        ("license", "curl -s -b /tmp/latest_cookies.txt http://127.0.0.1:8000/api/v1/license/status"),
        ("前端bundle", "grep -o 'assets/index-[A-Za-z0-9_]*\\.js' /home/admin1/aiops-platform/frontend/dist/index.html"),
        ("uvicorn实例数", "pgrep -fc 'uvicorn app.main:app'"),
    ]
    for name, cmd in checks:
        out, _, _ = run(cmd)
        print(f"  {name}: {out[:120]}")

    sftp.close()
    client.close()
    print("完成。")


if __name__ == "__main__":
    main()
