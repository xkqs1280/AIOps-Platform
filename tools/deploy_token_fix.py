# -*- coding: utf-8 -*-
"""部署「重启后 token 失效/页面空白不跳登录」修复到远程服务器。

覆盖文件：
  backend/app/config.py    SECRET_KEY 自动持久化到 .env
  backend/app/main.py      根路径校验 token 有效性
  frontend/dist/*          前端 401 拦截跳登录
流程：备份 -> 上传 -> 干净重启 uvicorn -> 验证。
"""
import os
import sys
import time

import paramiko

# 连接凭据从环境变量读取（勿硬编码进仓库）
HOST = os.environ.get("AIOPS_DEPLOY_HOST", "<server-ip>")
USER = os.environ.get("AIOPS_DEPLOY_USER", "admin")
PWD = os.environ.get("AIOPS_DEPLOY_PASSWORD", "")
BASE = "/opt/aiops-platform"
LOCAL = r"d:\WorkBuddy\codex\AIOps"

if not PWD:
    print("未设置 AIOPS_DEPLOY_PASSWORD 环境变量，拒绝执行。")
    sys.exit(1)

FILES = [
    os.path.join(LOCAL, "backend", "app", "config.py"),
    os.path.join(LOCAL, "backend", "app", "main.py"),
]
DIST = os.path.join(LOCAL, "frontend", "frontend", "dist")


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
    bk = f"{BASE}/_backup_token_fix_{ts}"
    print(f"[1/5] 备份到 {bk}")
    run(f"mkdir -p {bk}/app {bk}/dist")

    # 2. 上传后端文件
    print("[2/5] 上传后端 config.py / main.py")
    for f in FILES:
        remote = f"{BASE}/backend/app/{os.path.basename(f)}"
        run(f"cp {remote} {bk}/app/")
        sftp.put(f, remote)
        print(f"  -> {remote}")

    # 3. 替换前端 dist（先删旧 assets，避免 hash 残留）
    print("[3/5] 替换前端 dist")
    run(f"cp -r {BASE}/frontend/dist {bk}/dist/")
    run(f"rm -rf {BASE}/frontend/dist/assets")
    for root, _dirs, files in os.walk(DIST):
        for name in files:
            local = os.path.join(root, name)
            rel = os.path.relpath(local, DIST).replace("\\", "/")
            remote = f"{BASE}/frontend/dist/{rel}"
            try:
                sftp.mkdir(os.path.dirname(remote))
            except IOError:
                pass
            sftp.put(local, remote)
    print(f"  dist 共上传 {len([f for _r,_d,fs in os.walk(DIST) for f in fs])} 个文件")

    # 4. 干净重启 uvicorn（杀掉所有实例，避免双进程竞争）
    print("[4/5] 重启 uvicorn")
    run("pkill -f 'uvicorn app.main:app' || true")
    time.sleep(2)
    run(f"cd {BASE}/backend && nohup .venv/bin/python3 .venv/bin/uvicorn app.main:app "
        f"--host 0.0.0.0 --port 8000 > {BASE}/backend/uvicorn.log 2>&1 &")
    # 等待启动
    ok = False
    for _ in range(30):
        time.sleep(1)
        out, _, rc = run("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/health")
        if out.strip() == "200":
            ok = True
            break
    if not ok:
        print("[FAIL] uvicorn 未就绪，日志尾部：")
        print(run(f"tail -30 {BASE}/backend/uvicorn.log")[0])
        sys.exit(1)
    print("  uvicorn 已启动")

    # 5. 验证
    print("[5/5] 验证")
    # a. 无 cookie 访问 / -> 303 跳 /login
    out, _, rc = run("curl -s -o /dev/null -w '%{http_code} %{redirect_url}' http://127.0.0.1:8000/")
    print(f"  无cookie访问 /: {out}")
    # b. 登录拿 cookie
    admin_pwd = os.environ.get("AIOPS_DEPLOY_ADMIN_PASSWORD", "<admin-password>")
    out, _, _ = run(f"curl -s -c /tmp/tf_cookies.txt -X POST http://127.0.0.1:8000/api/v1/auth/login "
                    "-H 'Content-Type: application/json' "
                    "-d '{{\\\"username\\\":\\\"admin\\\",\\\"password\\\":\\\"{admin_pwd}\\\"}}'")
    print(f"  login: {out[:80]}")
    # c. 带 cookie 访问 API
    out, _, rc = run("curl -s -b /tmp/tf_cookies.txt -o /dev/null -w '%{http_code}' "
                     "http://127.0.0.1:8000/api/v1/devices")
    print(f"  带cookie访问 devices: {out}")
    # d. 伪造失效 cookie 访问 / -> 303 跳 /login 且 cookie 被清
    out, _, rc = run("curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "
                     "--cookie 'access_token=invalid.token.here' http://127.0.0.1:8000/")
    print(f"  伪造token访问 /: {out}")
    # e. .env 已写入 SECRET_KEY
    out, _, rc = run("grep -c '^SECRET_KEY=' /home/admin1/aiops-platform/backend/.env")
    print(f"  .env SECRET_KEY 行数: {out.strip()}")

    sftp.close()
    client.close()
    print("完成。")


if __name__ == "__main__":
    main()
