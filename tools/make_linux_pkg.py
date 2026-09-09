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
io.open(os.path.join(PKG_DIR, "backend", ".env.example"), "w", encoding="utf-8", newline="\n").write(env_example)

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
# ---- 行尾自愈：若本脚本被 Windows 工具改写为 CRLF（\r\n），先转回 LF 再执行，避免 \r 造成语法错误 ----
[ -z "$(grep -q $'\r' "$0" && echo x)" ] || { sed -i 's/\r$//' "$0"; exec bash "$0" "$@"; } # guard end
# ============================================
#  AIOps Platform  Linux 一键安装脚本
#  适用: Ubuntu 22.04+ / Debian 12+ / Rocky·AlmaLinux 9+
#  功能: 自动检查/安装 Python 3.10+ → 下载 Python 依赖 → 准备 PostgreSQL
#        → 生成 .env → 启动服务 → 打印访问地址 / 登录账号 / 密码
#  (CentOS 7 不支持：自带 Python 3.6 过低且已 EOL)
# ============================================
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

say()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '  \033[33m[警告] %s\033[0m\n' "$*"; }
die()  { printf '\033[31m[错误] %s\033[0m\n' "$*" >&2; exit 1; }

# ---------- 0 环境识别 ----------
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
if [ -r /etc/os-release ]; then . /etc/os-release; fi
DISTRO="${ID:-unknown}"; VER="${VERSION_ID:-}"
info "系统: ${PRETTY_NAME:-unknown} | 模式: $([ -z "$SUDO" ] && echo root || echo 普通用户+sudo)"
if command -v apt-get >/dev/null 2>&1; then PKG=apt
elif command -v dnf    >/dev/null 2>&1; then PKG=dnf
elif command -v yum    >/dev/null 2>&1; then PKG=yum
else PKG=""; fi

port_open() {
  if command -v ss >/dev/null 2>&1; then ss -tln 2>/dev/null | grep -qE '[:.]5432\b'
  elif command -v netstat >/dev/null 2>&1; then netstat -tln 2>/dev/null | grep -qE '[:.]5432\b'
  else return 1; fi
}

# ---------- 1/6 Python ----------
say "1/6 检查 / 安装 Python 3.10+"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)' >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  case "$PKG" in
    apt)
      info "系统 Python 过低，尝试安装新版 Python 与 venv 组件…"
      $SUDO env DEBIAN_FRONTEND=noninteractive apt-get update -qq
      for c in python3.13 python3.12 python3.11 python3.10 python3; do
        if [ "$c" = python3 ] || apt-cache policy "$c" 2>/dev/null | grep -q Candidate; then
          $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y "$c" "$c-venv" >/dev/null 2>&1 && { PY="$c"; break; } || true
        fi
      done
      [ -z "$PY" ] && { $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv >/dev/null 2>&1 && PY=python3 || true; }
      ;;
    dnf)
      info "系统 Python 过低，尝试安装 python3.11/3.12…"
      for c in python3.12 python3.11 python3; do
        $SUDO dnf install -y "$c" "$c-pip" >/dev/null 2>&1 && { PY="$c"; break; } || true
      done
      ;;
    yum)
      die "检测到 CentOS 7 / 旧版 yum 系统：自带 Python 3.6 过低且仓库无 3.10+，平台无法运行。建议改用 Rocky Linux 9 / AlmaLinux 9 / Ubuntu 22.04+。"
      ;;
    *)
      die "无法识别系统包管理器，请先手动安装 Python 3.10+ 后重新运行本脚本。"
      ;;
  esac
fi
[ -z "$PY" ] && die "Python 3.10+ 安装失败，请检查上方日志或手动安装后重试。"
"$PY" -c 'import sys; assert sys.version_info >= (3,10), "需要 Python 3.10+"'
PYV="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
info "使用 Python: $PY ($PYV)"

# ---------- 2/6 venv + 依赖 ----------
say "2/6 创建虚拟环境并下载依赖（首次约 3~8 分钟，视网速而定）"
if [ ! -x backend/.venv/bin/python ]; then
  info "创建虚拟环境 backend/.venv …"
  if ! "$PY" -m venv backend/.venv; then
    warn "venv 创建失败（可能缺 venv 组件），尝试补装后重试…"
    case "$PKG" in
      apt) $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y "$PY-venv" python3-venv >/dev/null 2>&1 || true ;;
      dnf) $SUDO dnf install -y "$PY-pip" python3-pip >/dev/null 2>&1 || true ;;
    esac
    "$PY" -m venv backend/.venv || die "虚拟环境创建失败：请先安装 $PY-venv（或 python3-venv / python3-pip）再运行"
  fi
fi
PIP="backend/.venv/bin/pip"
export PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=5
"$PIP" install --upgrade pip -q >/dev/null 2>&1 || true
pip_install() {
  if "$PIP" install "$@"; then return 0; fi
  if [ -z "${PIP_INDEX_URL:-}" ]; then
    warn "官方 PyPI 下载失败，自动改用清华镜像重试…"
    "$PIP" install -i https://pypi.tuna.tsinghua.edu.cn/simple "$@"
  else
    return 1
  fi
}
info "安装 bcrypt==4.0.1（passlib 兼容，必须锁版本）…"
pip_install "bcrypt==4.0.1" -q || die "bcrypt 安装失败"
info "下载并安装后端依赖 backend/requirements.txt …"
pip_install -r backend/requirements.txt || die "后端依赖下载/安装失败，请检查网络后重新运行本脚本"
info "依赖安装完成"

# ---------- 3/6 PostgreSQL ----------
say "3/6 准备 PostgreSQL（优先: 已有实例 -> Docker -> 系统包安装）"
DB_OK=0
if port_open; then
  DB_OK=1
  info "检测到 127.0.0.1:5432 已有 PostgreSQL，将直接使用"
elif command -v docker >/dev/null 2>&1; then
  if ! docker info >/dev/null 2>&1; then
    info "启动 Docker 服务…"
    $SUDO systemctl enable --now docker >/dev/null 2>&1 || $SUDO service docker start >/dev/null 2>&1 || true
    sleep 2
  fi
  if docker info >/dev/null 2>&1; then
    if ! docker ps -a --format '{{.Names}}' | grep -q '^aiops-postgres$'; then
      info "通过 Docker 启动 PostgreSQL 16（镜像 postgres:16-alpine）…"
      docker run -d --name aiops-postgres \
        -e POSTGRES_USER=aiops -e POSTGRES_PASSWORD=aiops123 -e POSTGRES_DB=aiops \
        -p 5432:5432 -v aiops-pgdata:/var/lib/postgresql/data \
        --restart unless-stopped postgres:16-alpine >/dev/null 2>&1 || \
      $SUDO docker run -d --name aiops-postgres \
        -e POSTGRES_USER=aiops -e POSTGRES_PASSWORD=aiops123 -e POSTGRES_DB=aiops \
        -p 5432:5432 -v aiops-pgdata:/var/lib/postgresql/data \
        --restart unless-stopped postgres:16-alpine >/dev/null 2>&1 || true
    else
      docker start aiops-postgres >/dev/null 2>&1 || $SUDO docker start aiops-postgres >/dev/null 2>&1 || true
    fi
    for i in $(seq 1 40); do
      if docker exec aiops-postgres pg_isready -U aiops >/dev/null 2>&1 || $SUDO docker exec aiops-postgres pg_isready -U aiops >/dev/null 2>&1; then DB_OK=1; break; fi
      sleep 1
    done
    [ "$DB_OK" = 1 ] && info "Docker PostgreSQL 已就绪 (容器 aiops-postgres)"
  fi
fi
if [ "$DB_OK" = 0 ] && [ -n "$PKG" ]; then
  info "通过系统包管理器安装 PostgreSQL（下载安装约 1~3 分钟）…"
  case "$PKG" in
    apt)
      $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql postgresql-client >/dev/null 2>&1 || true
      $SUDO systemctl enable --now postgresql >/dev/null 2>&1 || $SUDO service postgresql start >/dev/null 2>&1 || true
      sleep 2
      ;;
    dnf)
      $SUDO dnf install -y postgresql-server postgresql >/dev/null 2>&1 || true
      if [ ! -d /var/lib/pgsql/data/base ]; then
        $SUDO /usr/bin/postgresql-setup --initdb >/dev/null 2>&1 || $SUDO postgresql-setup --initdb >/dev/null 2>&1 || true
      fi
      $SUDO systemctl enable --now postgresql >/dev/null 2>&1 || true
      sleep 2
      HBA="$(find /var/lib/pgsql -name pg_hba.conf 2>/dev/null | head -n 1)"
      if [ -n "$HBA" ]; then
        info "调整 pg_hba.conf：host 认证 ident -> scram-sha-256"
        $SUDO sed -i 's/\bident\b/scram-sha-256/g' "$HBA" || true
        $SUDO systemctl reload postgresql >/dev/null 2>&1 || true
      fi
      ;;
  esac
  for i in $(seq 1 30); do port_open && { DB_OK=1; break; }; sleep 1; done
  if [ "$DB_OK" = 1 ]; then
    if $SUDO -u postgres psql -tAc 'SELECT 1' >/dev/null 2>&1; then
      $SUDO -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='aiops'" 2>/dev/null | grep -q 1 || \
        $SUDO -u postgres psql -c "SET password_encryption='scram-sha-256'; CREATE USER aiops WITH PASSWORD 'aiops123' SUPERUSER;" >/dev/null 2>&1 || true
      $SUDO -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='aiops'" 2>/dev/null | grep -q 1 || \
        $SUDO -u postgres psql -c "CREATE DATABASE aiops OWNER aiops;" >/dev/null 2>&1 || true
    fi
    info "系统 PostgreSQL 已就绪（库/用户 aiops/aiops123，密码连接）"
  fi
fi
if [ "$DB_OK" = 1 ] && $SUDO -u postgres psql -tAc 'SELECT 1' >/dev/null 2>&1; then
  $SUDO -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='aiops'" 2>/dev/null | grep -q 1 || \
    $SUDO -u postgres psql -c "CREATE USER aiops WITH PASSWORD 'aiops123' SUPERUSER;" >/dev/null 2>&1 || true
  $SUDO -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='aiops'" 2>/dev/null | grep -q 1 || \
    $SUDO -u postgres psql -c "CREATE DATABASE aiops OWNER aiops;" >/dev/null 2>&1 || true
fi
[ "$DB_OK" = 1 ] || die "PostgreSQL 未能就绪。请先安装 PostgreSQL（或 Docker）后重新运行本脚本，详见 README。"

# ---------- 4/6 .env ----------
say "4/6 生成运行配置 backend/.env"
if [ ! -f backend/.env ]; then
  SECRET="$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
  FERNET="$(backend/.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' 2>/dev/null || head -c 32 /dev/urandom | base64 | tr '+/' '-_')"
  ADMIN_USER="${BOOTSTRAP_ADMIN_USERNAME:-admin}"
  if [ -n "${BOOTSTRAP_ADMIN_PASSWORD:-}" ]; then
    ADMIN_PASS="$BOOTSTRAP_ADMIN_PASSWORD"
  else
    ADMIN_PASS="$(head -c 12 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 12)"
  fi
  [ -z "$ADMIN_PASS" ] && ADMIN_PASS="Aiop$(date +%s | tail -c 8)"
  cat > backend/.env <<EOF2
DATABASE_URL=postgresql+psycopg_async://aiops:aiops123@127.0.0.1:5432/aiops
SECRET_KEY=${SECRET}
CREDENTIAL_ENCRYPTION_KEY=${FERNET}
BOOTSTRAP_ADMIN_USERNAME=${ADMIN_USER}
BOOTSTRAP_ADMIN_PASSWORD=${ADMIN_PASS}
LICENSE_ENABLED=true
EOF2
  info "已生成 backend/.env"
else
  warn "backend/.env 已存在，保留原配置"
fi
ADMIN_USER="$(grep -E '^BOOTSTRAP_ADMIN_USERNAME=' backend/.env | tail -n 1 | cut -d= -f2-)"
ADMIN_PASS="$(grep -E '^BOOTSTRAP_ADMIN_PASSWORD=' backend/.env | tail -n 1 | cut -d= -f2-)"
printf 'AIOps 平台登录凭证\n访问地址: http://<本机IP>:8000\n登录账号: %s\n登录密码: %s\n' "$ADMIN_USER" "$ADMIN_PASS" > admin-credentials.txt
chmod 600 admin-credentials.txt

# ---------- 5/6 启动 ----------
say "5/6 启动 AIOps 服务"
pkill -f '[u]vicorn app.main:app' 2>/dev/null || true
sleep 1
cd backend
nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 >> uvicorn.log 2>&1 &
cd "$ROOT"
OK=0
for i in $(seq 1 20); do
  if (command -v ss >/dev/null 2>&1 && ss -tln 2>/dev/null | grep -q ':8000') || (command -v netstat >/dev/null 2>&1 && netstat -tln 2>/dev/null | grep -q ':8000'); then OK=1; break; fi
  sleep 1
done
if [ "$OK" = 1 ]; then
  info "服务已启动（日志: backend/uvicorn.log）"
  command -v firewall-cmd >/dev/null 2>&1 && { $SUDO firewall-cmd --permanent --add-port=8000/tcp >/dev/null 2>&1 || true; $SUDO firewall-cmd --reload >/dev/null 2>&1 || true; }
  command -v ufw >/dev/null 2>&1 && { $SUDO ufw allow 8000/tcp >/dev/null 2>&1 || true; }
else
  warn "端口 8000 未监听成功，请查看 backend/uvicorn.log；也可稍后手动运行 ./start.sh"
fi

# ---------- 6/6 完成 ----------
say "6/6 安装完成"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$IP" ] && IP="127.0.0.1"
echo ""
echo "=============================================="
echo "  AIOps 平台 安装完成"
echo ""
echo "  访问地址 : http://${IP}:8000   (本机: http://127.0.0.1:8000)"
echo "  登录账号 : ${ADMIN_USER}"
echo "  登录密码 : ${ADMIN_PASS}"
echo ""
echo "  凭证备份 : $(pwd)/admin-credentials.txt"
echo "  日常运维 : ./start.sh 启动 / ./stop.sh 停止"
echo "  运行日志 : backend/uvicorn.log"
echo "  提示     : 首次登录后请在「授权管理」页激活授权（试用版 3 个月 / 全功能版永久）"
echo "=============================================="'''
io.open(os.path.join(PKG_DIR, "install.sh"), "w", encoding="utf-8", newline="\n").write(install_sh)

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
io.open(os.path.join(PKG_DIR, "start.sh"), "w", encoding="utf-8", newline="\n").write(start_sh)

# ---------------- stop.sh ----------------
stop_sh = '#!/usr/bin/env bash\npkill -f "[u]vicorn app.main:app" 2>/dev/null && echo "AIOps 已停止" || echo "AIOps 未在运行"\n'
io.open(os.path.join(PKG_DIR, "stop.sh"), "w", encoding="utf-8", newline="\n").write(stop_sh)

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
io.open(os.path.join(PKG_DIR, "README.md"), "w", encoding="utf-8", newline="\n").write(readme)

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
