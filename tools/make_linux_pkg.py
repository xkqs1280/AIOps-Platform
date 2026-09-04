"""制作 AIOps v4.4.0 Linux 生产包（源码部署）"""
import io
import os
import shutil
import zipfile

ROOT = r"D:\WorkBuddy\codex\AIOps"
STAGE = os.path.join(ROOT, "dist", "aiops-v4.4.0-linux")
PKG_DIR = os.path.join(STAGE, "AIOps")
OUT = os.path.join(ROOT, "dist", "aiops-v4.4.0-linux.zip")

# 清空 staging
if os.path.isdir(STAGE):
    shutil.rmtree(STAGE, ignore_errors=True)
os.makedirs(os.path.join(PKG_DIR, "backend"), exist_ok=True)
os.makedirs(os.path.join(PKG_DIR, "frontend"), exist_ok=True)


def copy_tree(src, dst, exclude_dirs=()):
    count = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        rel = os.path.relpath(root, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for f in files:
            shutil.copy2(os.path.join(root, f), os.path.join(target, f))
            count += 1
    return count


print("== 复制后端源码 ==")
n = copy_tree(os.path.join(ROOT, "backend", "app"), os.path.join(PKG_DIR, "backend", "app"),
              exclude_dirs=("__pycache__",))
print("  backend/app 文件:", n)
shutil.copy2(os.path.join(ROOT, "backend", "requirements.txt"),
             os.path.join(PKG_DIR, "backend", "requirements.txt"))

# .env.example
env_example = """# AIOps v4.4.0 环境配置模板（复制为 .env 后按需修改）
# PostgreSQL 连接串
DATABASE_URL=postgresql+psycopg_async://aiops:aiops123@localhost:5432/aiops
# 会话密钥：留空即可，首次启动自动生成随机密钥并写回 .env（重启不失效；删除后重启将使所有登录失效）
SECRET_KEY=
# 设备凭据加密密钥（Fernet key；不配置则设备密码明文存储，生产务必设置）
CREDENTIAL_ENCRYPTION_KEY=replace-with-a-fernet-key
# 首次启动自动创建的管理员账号（安装脚本会自动替换为随机密码；此处仅占位）
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=change-me-after-install
# 授权模块开关（true 时未激活/测试版到期会锁定平台）
LICENSE_ENABLED=true
# 跨域来源（逗号分隔）
CORS_ORIGINS=http://localhost:8000,http://127.0.0.1:8000
"""
io.open(os.path.join(PKG_DIR, "backend", ".env.example"), "w", encoding="utf-8").write(env_example)

print("== 复制前端 dist（v4.4.0） ==")
n = copy_tree(os.path.join(ROOT, "frontend", "dist"), os.path.join(PKG_DIR, "frontend", "dist"))
print("  frontend/dist 文件:", n)

print("== 复制升级脚本 ==")
os.makedirs(os.path.join(PKG_DIR, "deploy"), exist_ok=True)
upgrade_sh = os.path.join(ROOT, "deploy", "upgrade_apply.sh")
if os.path.isfile(upgrade_sh):
    shutil.copy2(upgrade_sh, os.path.join(PKG_DIR, "deploy", "upgrade_apply.sh"))
    print("  deploy/upgrade_apply.sh 已复制")
else:
    print("  [!] 未找到 deploy/upgrade_apply.sh")

# ---------------- install.sh ----------------
install_sh = r'''#!/usr/bin/env bash
# ============================================
#  AIOps v4.4.0  Linux 一键安装脚本
#  适用: Ubuntu 22.04+ / Debian 12+ / CentOS 9+
# ============================================
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "== 1/4 检查 Python =="
if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 未找到 python3，请先安装："
  echo "  Ubuntu/Debian: sudo apt install -y python3 python3-venv python3-pip"
  echo "  CentOS:        sudo dnf install -y python3 python3-pip"
  exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3,10), "需要 Python 3.10+"; print("  Python", sys.version.split()[0], "OK")'

echo "== 2/4 创建虚拟环境并安装依赖（约3-5分钟） =="
if [ ! -d backend/.venv ]; then
  python3 -m venv backend/.venv
fi
backend/.venv/bin/pip install --upgrade pip -q
# passlib 1.7.4 与 bcrypt>=5 不兼容，必须锁 4.0.1
backend/.venv/bin/pip install "bcrypt==4.0.1" -q
backend/.venv/bin/pip install -r backend/requirements.txt

echo "== 3/4 检查 PostgreSQL =="
DB_OK=0
if command -v ss >/dev/null 2>&1 && ss -tln 2>/dev/null | grep -q ':5432'; then
  DB_OK=1
  echo "  检测到 5432 端口已有 PostgreSQL，使用现有实例（需存在库/用户 aiops/aiops123）"
elif command -v docker >/dev/null 2>&1; then
  if ! docker ps --format '{{.Names}}' | grep -q '^aiops-postgres$'; then
    echo "  通过 Docker 启动 PostgreSQL…"
    docker run -d --name aiops-postgres \
      -e POSTGRES_USER=aiops -e POSTGRES_PASSWORD=aiops123 -e POSTGRES_DB=aiops \
      -p 5432:5432 -v aiops-pgdata:/var/lib/postgresql/data \
      --restart unless-stopped postgres:16-alpine
    echo "  Docker PostgreSQL 已启动 (aiops-postgres / aiops:aiops123)"
  fi
  # 等待就绪
  for i in $(seq 1 30); do
    if docker exec aiops-postgres pg_isready -U aiops >/dev/null 2>&1; then DB_OK=1; break; fi
    sleep 1
  done
  [ "$DB_OK" = "1" ] && echo "  PostgreSQL 就绪"
else
  echo "[警告] 未检测到 PostgreSQL。请先安装 PostgreSQL 并创建数据库："
  echo "  sudo apt install -y postgresql && sudo -u postgres psql -c \"CREATE USER aiops PASSWORD 'aiops123' SUPERUSER; CREATE DATABASE aiops OWNER aiops;\""
  echo "  或安装 docker 后重新运行本脚本。"
  exit 1
fi

echo "== 4/4 生成配置 .env =="
if [ ! -f backend/.env ]; then
  SECRET="$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
  # 用项目 venv 的 cryptography 生成合法 Fernet key（base64url 32 字节）
  FERNET="$(backend/.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' 2>/dev/null || head -c 32 /dev/urandom | base64 | tr '+/' '-_')"
  # 管理员随机密码（含大小写+数字，>=12 位）
  ADMIN_PASS="$(head -c 12 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 12)"
  [ -z "$ADMIN_PASS" ] && ADMIN_PASS="Aiop$(date +%s | tail -c 8)"
  cat > backend/.env <<EOF
DATABASE_URL=postgresql+psycopg_async://aiops:aiops123@localhost:5432/aiops
SECRET_KEY=${SECRET}
CREDENTIAL_ENCRYPTION_KEY=${FERNET}
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=${ADMIN_PASS}
LICENSE_ENABLED=true
EOF
  echo "  已生成 backend/.env（管理员 admin / ${ADMIN_PASS}，设备凭据已启用加密）"
else
  echo "  backend/.env 已存在，跳过"
fi

echo ""
echo "=============================================="
echo "  安装完成！"
echo "  启动服务: ./start.sh"
echo "  停止服务: ./stop.sh"
echo "  访问地址: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo '<本机IP>'):8000"
echo "  默认账号: admin / ${ADMIN_PASS}"
echo "  首次登录后请前往「授权管理」页激活授权（测试版3个月 / 全功能版永久）"
echo "=============================================="
'''
io.open(os.path.join(PKG_DIR, "install.sh"), "w", encoding="utf-8").write(install_sh)

# ---------------- start.sh ----------------
start_sh = r'''#!/usr/bin/env bash
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT/backend"
pkill -f '[u]vicorn app.main:app' 2>/dev/null || true
sleep 1
nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 >> uvicorn.log 2>&1 &
sleep 2
if pgrep -f '[u]vicorn app.main:app' >/dev/null; then
  IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  echo "AIOps v4.4.0 已启动: http://${IP:-127.0.0.1}:8000  （日志: backend/uvicorn.log）"
else
  echo "启动失败，查看日志: backend/uvicorn.log"
  tail -20 uvicorn.log
fi
'''
io.open(os.path.join(PKG_DIR, "start.sh"), "w", encoding="utf-8").write(start_sh)

# ---------------- stop.sh ----------------
stop_sh = '#!/usr/bin/env bash\npkill -f "[u]vicorn app.main:app" 2>/dev/null && echo "AIOps 已停止" || echo "AIOps 未在运行"\n'
io.open(os.path.join(PKG_DIR, "stop.sh"), "w", encoding="utf-8").write(stop_sh)

print("== 写 README ==")
readme = """# AIOps 智能运维托管平台 v4.4.0

网络及安全设备 7×24 智能监控与故障预测平台。包含：监控大屏、设备管理、告警、拓扑、配置备份、H3C 巡检、重要业务监控、生命周期、安全监控、等保合规、**平台授权**（测试版/全功能版）等模块。

- **测试版**：功能全开，有效期 3 个月，到期后平台锁定（仅授权页可用），到期前 30 天预警
- **全功能版**：永久授权
- 授权联系邮箱：**x1280455974@163.com**

---

## 一、Windows 部署（生产包）

### 方式 A：解压版（推荐，无需安装 Python）
1. 解压 `aiops-v4.0.zip` 到任意目录（如 `D:\\AIOps`）
2. 双击 **`一键部署.bat`**：自动安装 PostgreSQL + 启动服务
3. 浏览器访问 **http://本机IP:8000**，默认账号 `admin`（初始密码见 `backend\.env` 的 `BOOTSTRAP_ADMIN_PASSWORD`）
4. 若数据库连接失败，运行 `deploy\\fix_after_upgrade.bat`（自动修复连接串）

### 方式 B：源码运行（开发/调试）
```
python -m venv .venv
.venv\\Scripts\\activate
pip install -r backend\\requirements.txt
python start_dev.py
```

### 升级覆盖
- 解压新版覆盖旧目录（**保留 backend/.env 和 encryption.key**）
- 加密 key 复用不丢设备凭据；连库密码非 postgres 时跑 `fix_after_upgrade.bat`

---

## 二、Linux 部署（源码包 `aiops-v4.4.0-linux.zip`）

### 环境要求
- Ubuntu 22.04+ / Debian 12+ / CentOS 9+，Python 3.10+
- PostgreSQL（脚本可自动用 Docker 启动）或已安装的 PostgreSQL

### 安装步骤
```bash
# 1. 解压
unzip aiops-v4.4.0-linux.zip && cd AIOps

# 2. 一键安装（建 venv + 装依赖 + 准备 PostgreSQL + 生成 .env）
chmod +x install.sh && ./install.sh

# 3. 启动
./start.sh

# 4. 访问
# http://<本机IP>:8000   （默认账号 admin，密码为安装时生成并打印的随机密码）
```

### 手动安装（不依赖一键脚本）
```bash
cd AIOps/backend
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install "bcrypt==4.0.1"   # passlib 兼容，必须
.venv/bin/pip install -r requirements.txt
# 准备 PostgreSQL（库/用户 aiops/aiops123，或改 .env 的 DATABASE_URL）
cp .env.example .env   # 修改 SECRET_KEY 与数据库连接
nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 >> uvicorn.log 2>&1 &
```

### systemd 服务（可选）
```ini
# /etc/systemd/system/aiops.service
[Unit]
Description=AIOps Platform
After=network.target postgresql.service
[Service]
WorkingDirectory=/opt/AIOps/backend
ExecStart=/opt/AIOps/backend/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
User=admin1
[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now aiops
```

---

## 三、平台授权激活（两种部署方式一致）

1. 登录后点击左侧「**授权管理**」
2. 复制页面上的**本机机器码**
3. 通过授权联系邮箱 **x1280455974@163.com** 联系厂商，或使用厂商的「AIOPS激活工具.exe」生成激活码
4. 将激活码粘贴到「授权管理」页 → 点**立即激活**
5. 激活后显示：授权版本 / 到期时间 / 剩余天数

> 激活码与机器码绑定，**更换服务器需重新申请**；测试版到期后平台自动锁定，续期只需重新激活。

---

## 四、常见问题
| 问题 | 处理 |
|---|---|
| 登录提示"请先登录" | 访问 http://IP:8000 会跳登录页，正常 |
| 提示"平台未授权/已锁定" | 前往「授权管理」输入激活码 |
| PostgreSQL 连接失败 | 检查 backend/.env 的 DATABASE_URL；Windows 跑 fix_after_upgrade.bat |
| 激活码"验签失败" | 激活工具必须与 vendor_keys 同目录使用 |
| 修改端口 | start.sh / 一键部署中调整 8000 并同步 .env CORS |

---
*© 2026 AIOps Platform v4.4.0*
"""
io.open(os.path.join(PKG_DIR, "README.md"), "w", encoding="utf-8").write(readme)

print("== 打包 zip ==")
z = zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED)
for root, dirs, files in os.walk(PKG_DIR):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for f in files:
        full = os.path.join(root, f)
        rel = os.path.relpath(full, STAGE)
        zi = zipfile.ZipInfo(rel.replace("\\", "/"))
        # 脚本类文件设置 unix 可执行位（0755），解压后可直接执行
        zi.external_attr = (0o755 if f.endswith(".sh") else 0o644) << 16
        zi.compress_type = zipfile.ZIP_DEFLATED
        with open(full, "rb") as fh:
            z.writestr(zi, fh.read())
z.close()
sz = os.path.getsize(OUT) / 1024 / 1024
print(f"Linux 包: {OUT} ({sz:.1f}MB)")
