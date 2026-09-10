"""制作 AIOps v4.5.1 Linux 生产包（源码部署）"""
import io
import os
import shutil
import zipfile

ROOT = r"D:\WorkBuddy\codex\AIOps"
STAGE = os.path.join(ROOT, "dist", "aiops-v4.5.1-linux")
PKG_DIR = os.path.join(STAGE, "AIOps")
OUT = os.path.join(ROOT, "dist", "aiops-v4.5.1-linux.zip")

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
env_example = """# AIOps v4.5.1 环境配置模板（复制为 .env 后按需修改）
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

print("== 复制前端 dist（v4.5.1） ==")
n = copy_tree(os.path.join(ROOT, "frontend", "dist"), os.path.join(PKG_DIR, "frontend", "dist"))
print("  frontend/dist 文件:", n)

print("== 复制 deploy 脚本 ==")
os.makedirs(os.path.join(PKG_DIR, "deploy"), exist_ok=True)
for _fname in ("upgrade_apply.sh", "reset_admin.sh"):
    _src = os.path.join(ROOT, "deploy", _fname)
    if os.path.isfile(_src):
        shutil.copy2(_src, os.path.join(PKG_DIR, "deploy", _fname))
        print("  deploy/%s 已复制" % _fname)
    else:
        print("  [!] 未找到 deploy/%s" % _fname)

# ---------------- install.sh ----------------
install_sh = r'''#!/usr/bin/env bash
# ---- 行尾自愈：若本脚本被 Windows 工具改写为 CRLF（\r\n），先转回 LF 再执行，避免 \r 造成语法错误 ----
[ -z "$(grep -q $'\r' "$0" && echo x)" ] || { sed -i 's/\r$//' "$0"; exec bash "$0" "$@"; } # guard end
# ============================================
#  AIOps Platform  Linux 一键安装脚本
#  适用: Ubuntu 22.04+ / Debian 12+ / Rocky·AlmaLinux 9+
#  功能: 自动检查/安装 Python 3.10+ → 下载 Python 依赖 → 准备 PostgreSQL
#        → 生成 .env → 注册 systemd 开机自启 → 启动服务 → 打印访问地址 / 登录账号 / 密码
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
say "1/7 检查 / 安装 Python 3.10+"
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
say "2/7 创建虚拟环境并下载依赖（首次约 3~8 分钟，视网速而定）"

# 编译环境自检：Python 3.12+ 部分依赖（如 numpy）无预编译 wheel 时需源码编译，
# 必须保证 gcc + Python 头文件(python*-dev) 可用，否则会报 Unknown compiler / Python.h not found
CC_MISSING=0; DEV_MISSING=0
command -v cc >/dev/null 2>&1 || command -v gcc >/dev/null 2>&1 || CC_MISSING=1
PY_INC="$("$PY" -c 'import sysconfig;print(sysconfig.get_paths()["include"])' 2>/dev/null)"
{ [ -n "$PY_INC" ] && [ -f "$PY_INC/Python.h" ]; } || DEV_MISSING=1
if [ "$CC_MISSING" = 1 ] || [ "$DEV_MISSING" = 1 ]; then
  info "检测到缺少 C 编译环境（gcc / Python.h），部分依赖需源码编译，自动补装…"
  case "$PKG" in
    apt)
      $SUDO env DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
      $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y build-essential "$PY-dev" python3-dev >/dev/null 2>&1 || true
      ;;
    dnf)
      $SUDO dnf groupinstall -y "Development Tools" >/dev/null 2>&1 || $SUDO dnf install -y gcc gcc-c++ make >/dev/null 2>&1 || true
      $SUDO dnf install -y "$PY-devel" python3-devel >/dev/null 2>&1 || true
      ;;
  esac
  command -v cc >/dev/null 2>&1 || command -v gcc >/dev/null 2>&1 || \
    die "gcc 安装失败，请手动执行 $SUDO <包管理器> install build-essential 后重跑"
  if [ ! -f "/usr/include/$PYV/Python.h" ] && [ ! -f "$PY_INC/Python.h" ]; then
    die "Python $PYV 头文件安装失败，请手动执行 $SUDO <包管理器> install $PY-dev 后重跑"
  fi
  # 若虚拟环境在缺 dev 包时已创建（include 死链），删除重建以正确链接 Python.h
  if [ -d backend/.venv ] && { [ -z "$PY_INC" ] || [ ! -f "$PY_INC/Python.h" ]; }; then
    warn "已有虚拟环境缺少 Python 头文件链接，删除后重建…"
    rm -rf backend/.venv
  fi
  info "编译环境就绪"
fi

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
# 默认依赖源顺序（国内优先）：清华 -> 阿里云 -> 官方 PyPI（官方仅作最后兜底）
PIP_MIRRORS="https://pypi.tuna.tsinghua.edu.cn/simple https://mirrors.aliyun.com/pypi/simple/"
pip_install() {
  # 用户显式指定 PIP_INDEX_URL 时只用该源，不做镜像回退
  if [ -n "${PIP_INDEX_URL:-}" ]; then
    "$PIP" install "$@"
    return $?
  fi
  for m in $PIP_MIRRORS; do
    info "使用国内镜像源安装: ${m} …"
    if "$PIP" install -i "$m" "$@"; then return 0; fi
    warn "镜像 ${m} 下载失败，切换下一源…"
  done
  warn "国内镜像均失败，最后尝试官方 PyPI…"
  "$PIP" install "$@"
}
info "安装 bcrypt==4.0.1（passlib 兼容，必须锁版本）…"
pip_install "bcrypt==4.0.1" -q || die "bcrypt 安装失败"
info "下载并安装后端依赖 backend/requirements.txt …"
pip_install -r backend/requirements.txt || die "后端依赖下载/安装失败，请检查网络后重新运行本脚本"
info "依赖安装完成"

# ---------- 3/6 PostgreSQL ----------
say "3/7 准备 PostgreSQL（优先: 已有实例 -> Docker -> 系统包安装）"
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
      # 幂等：角色/库不存在则创建；存在也强制把密码对齐为 aiops123（防半状态/残留导致认证失败）
      $SUDO -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='aiops'" 2>/dev/null | grep -q 1 || \
        $SUDO -u postgres psql -c "CREATE USER aiops WITH PASSWORD 'aiops123' SUPERUSER;" >/dev/null 2>&1 || true
      $SUDO -u postgres psql -c "ALTER USER aiops WITH PASSWORD 'aiops123' SUPERUSER;" >/dev/null 2>&1 || true
      $SUDO -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='aiops'" 2>/dev/null | grep -q 1 || \
        $SUDO -u postgres psql -c "CREATE DATABASE aiops OWNER aiops;" >/dev/null 2>&1 || true
      $SUDO -u postgres psql -c "ALTER DATABASE aiops OWNER TO aiops;" >/dev/null 2>&1 || true
      PGPASSWORD=aiops123 psql -h 127.0.0.1 -U aiops -d aiops -tAc 'SELECT 1' >/dev/null 2>&1 && \
        info "PostgreSQL 就绪（库/用户 aiops/aiops123，密码连接验证通过）" || \
        warn "aiops 密码连接验证失败，请检查 pg_hba.conf（host 行应为 scram-sha-256）"
    fi
  fi
fi
# 兜底（覆盖「已有 5432 直接使用 / Docker」场景）：确保角色/库存在且密码与 .env 一致
if [ "$DB_OK" = 1 ] && $SUDO -u postgres psql -tAc 'SELECT 1' >/dev/null 2>&1; then
  $SUDO -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='aiops'" 2>/dev/null | grep -q 1 || \
    $SUDO -u postgres psql -c "CREATE USER aiops WITH PASSWORD 'aiops123' SUPERUSER;" >/dev/null 2>&1 || true
  $SUDO -u postgres psql -c "ALTER USER aiops WITH PASSWORD 'aiops123' SUPERUSER;" >/dev/null 2>&1 || true
  $SUDO -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='aiops'" 2>/dev/null | grep -q 1 || \
    $SUDO -u postgres psql -c "CREATE DATABASE aiops OWNER aiops;" >/dev/null 2>&1 || true
  $SUDO -u postgres psql -c "ALTER DATABASE aiops OWNER TO aiops;" >/dev/null 2>&1 || true
fi
[ "$DB_OK" = 1 ] || die "PostgreSQL 未能就绪。请先安装 PostgreSQL（或 Docker）后重新运行本脚本，详见 README。"

# ---------- 4/6 .env ----------
say "4/7 生成运行配置 backend/.env"
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

# ---------- 5/7 注册开机自启（systemd） ----------
say "5/7 注册开机自启服务"
SERVICE_OK=0
UNIT_FILE=/etc/systemd/system/aiops-backend.service
# 先清理历史上手工 nohup 起的裸进程，避免与 systemd 争抢 8000 端口导致反复重启
pkill -f '[u]vicorn app.main:app' 2>/dev/null || true
sleep 1
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  SYSD_VER="$(systemctl --version 2>/dev/null | head -n1 | awk '{print $2}')"
  if [ -n "$SYSD_VER" ] && [ "$SYSD_VER" -ge 240 ] 2>/dev/null; then
    LOG_DIRECTIVES="StandardOutput=append:${ROOT}/backend/uvicorn.log
StandardError=append:${ROOT}/backend/uvicorn.log"
  else
    LOG_DIRECTIVES="# 日志由 journald 收集: journalctl -u aiops-backend -f"
  fi
  TMP_UNIT="$(mktemp)"
  cat > "$TMP_UNIT" <<UNITEOF
[Unit]
Description=AIOps Platform Backend (v4.5.1)
Documentation=file://${ROOT}/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${ROOT}/backend
ExecStart=${ROOT}/backend/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 5
Restart=always
RestartSec=5
TimeoutStopSec=20
SuccessExitStatus=143
${LOG_DIRECTIVES}

[Install]
WantedBy=multi-user.target
UNITEOF
  if $SUDO cp "$TMP_UNIT" "$UNIT_FILE" 2>/dev/null; then
    if $SUDO systemctl daemon-reload >/dev/null 2>&1 && $SUDO systemctl enable aiops-backend >/dev/null 2>&1; then
      SERVICE_OK=1
      info "已注册 systemd 服务 aiops-backend（开机自启 + 进程崩溃自动拉起）"
    else
      warn "systemd 服务注册失败，将回退为普通进程方式启动"
    fi
  else
    warn "无权限写入 ${UNIT_FILE}，将回退为普通进程方式启动"
  fi
  rm -f "$TMP_UNIT"
else
  warn "当前环境无 systemd（容器 / 精简系统），跳过开机自启，回退为普通进程方式启动"
fi

# ---------- 6/7 启动 ----------
say "6/7 启动 AIOps 服务"
if [ "$SERVICE_OK" = 1 ]; then
  $SUDO systemctl restart aiops-backend >/dev/null 2>&1 || true
else
  cd backend
  nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 5 >> uvicorn.log 2>&1 &
  cd "$ROOT"
fi
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
  [ "$SERVICE_OK" = 1 ] && warn "可执行 systemctl status aiops-backend 查看失败原因"
fi

# ---------- 7/7 完成 ----------
chmod +x start.sh stop.sh 2>/dev/null || true
say "7/7 安装完成"
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
echo "  忘记密码 : sudo ./deploy/reset_admin.sh   (重置为 .env 中的初始密码，或追加参数指定新密码)"
if [ "$SERVICE_OK" = 1 ]; then
echo "  服务管理 : systemctl status|restart|stop aiops-backend  (已开机自启)"
echo "  运行日志 : journalctl -u aiops-backend -f  /  backend/uvicorn.log"
else
echo "  日常运维 : ./start.sh 启动 / ./stop.sh 停止  (未配置开机自启，重启后需手动执行)"
echo "  运行日志 : backend/uvicorn.log"
fi
echo "  提示     : 首次登录后请在「授权管理」页激活授权（试用版 3 个月 / 全功能版永久）"
echo "=============================================="
'''
io.open(os.path.join(PKG_DIR, "install.sh"), "w", encoding="utf-8", newline="\n").write(install_sh.replace("\r\n", "\n").lstrip("\n"))

# ---------------- start.sh ----------------
start_sh = r'''#!/usr/bin/env bash
# AIOps 平台启动脚本：若已注册 systemd 服务则走 systemctl（与开机自启一致），否则回退为直接启动
ROOT="$(cd "$(dirname "$0")" && pwd)"
SVC="aiops-backend"
if command -v systemctl >/dev/null 2>&1 && [ -f "/etc/systemd/system/${SVC}.service" ]; then
  if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
  if $SUDO systemctl restart "$SVC" >/dev/null 2>&1; then
    sleep 3
    if $SUDO systemctl is-active --quiet "$SVC"; then
      IP=$(hostname -I 2>/dev/null | awk '{print $1}')
      echo "AIOps v4.5.1 已启动: http://${IP:-127.0.0.1}:8000"
      echo "  服务: ${SVC}.service（已开机自启）  日志: journalctl -u ${SVC} -f"
      exit 0
    fi
  fi
  echo "systemd 服务未启动成功，回退为直接启动…"
fi
cd "$ROOT/backend"
pkill -f '[u]vicorn app.main:app' 2>/dev/null || true
sleep 1
nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 5 >> uvicorn.log 2>&1 &
# 端口探测确认成功（进程存活不代表应用启动成功，pgrep 会误报“已启动”）
OK=0
for i in $(seq 1 12); do
  if (command -v ss >/dev/null 2>&1 && ss -tln 2>/dev/null | grep -q ':8000') || \
     (command -v netstat >/dev/null 2>&1 && netstat -tln 2>/dev/null | grep -qE '[:.]8000\b'); then OK=1; break; fi
  sleep 1
done
if [ "$OK" = 1 ]; then
  IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  echo "AIOps v4.5.1 已启动: http://${IP:-127.0.0.1}:8000  （日志: backend/uvicorn.log）"
else
  echo "启动失败，查看日志: backend/uvicorn.log"
  tail -25 uvicorn.log
fi
'''
io.open(os.path.join(PKG_DIR, "start.sh"), "w", encoding="utf-8", newline="\n").write(start_sh.replace("\r\n", "\n").lstrip("\n"))

# ---------------- stop.sh ----------------
stop_sh = r'''#!/usr/bin/env bash
SVC="aiops-backend"
if command -v systemctl >/dev/null 2>&1 && [ -f "/etc/systemd/system/${SVC}.service" ]; then
  if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
  if $SUDO systemctl stop "$SVC" >/dev/null 2>&1; then
    echo "AIOps 已停止（systemd 服务 ${SVC}）"
    echo "  提示: 该服务已开机自启，下次重启机器仍会自动拉起；如需永久禁用请执行 systemctl disable ${SVC}"
    exit 0
  fi
fi
pkill -f "[u]vicorn app.main:app" 2>/dev/null && echo "AIOps 已停止" || echo "AIOps 未在运行"
'''
io.open(os.path.join(PKG_DIR, "stop.sh"), "w", encoding="utf-8", newline="\n").write(stop_sh.replace("\r\n", "\n").lstrip("\n"))

print("== 写 README ==")
readme = """# AIOps 智能运维托管平台 v4.5.1

网络及安全设备 7×24 智能监控与故障预测平台。包含：监控大屏、设备管理、告警、拓扑、配置备份、H3C 巡检、重要业务监控、生命周期、安全监控、等保合规、**平台授权**（测试版/全功能版）等模块。

- **测试版**：功能全开，有效期 3 个月，到期后平台锁定（仅授权页可用），到期前 30 天预警
- **全功能版**：永久授权
- 授权联系邮箱：**x1280455974@163.com**

---

## 一、Windows 部署（生产包）

### 方式 A：解压版（推荐，无需安装 Python）
1. 解压 `aiops-v4.0.zip` 到任意目录（如 `D:\\AIOps`）
2. 双击 **`一键部署.bat`**：自动安装 PostgreSQL + 启动服务
3. 浏览器访问 **http://本机IP:8000**，默认账号 `admin`（初始密码见 `backend/.env` 的 `BOOTSTRAP_ADMIN_PASSWORD`）
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

## 二、Linux 部署（源码包 `aiops-v4.5.1-linux.zip`）

### 环境要求
- Ubuntu 22.04+ / Debian 12+ / Rocky·AlmaLinux 9+（CentOS 7 已 EOL 且 Python 过低，不支持）
- 安装脚本会自动：检测/安装 Python 3.10+ → 下载 Python 依赖 → 准备 PostgreSQL（系统包或 Docker）→ 启动服务

### 安装步骤
```bash
# 1. 解压
unzip aiops-v4.5.1-linux.zip && cd AIOps

# 2. 一键安装：自动补环境 + 下载依赖 + 启动服务（约 5~10 分钟）
chmod +x install.sh && ./install.sh
#    完成后会打印 访问地址 / 登录账号 / 登录密码（同时保存到 admin-credentials.txt）

# 3. 访问（脚本已自动启动服务）
# http://<本机IP>:8000   （默认账号 admin，密码见结尾提示或 admin-credentials.txt）
# 安装脚本已自动注册开机自启：服务器重启后平台会自动拉起，无需手工启动
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
nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 5 >> uvicorn.log 2>&1 &
```

### 服务管理（安装脚本已自动注册 systemd 服务，无需手工配置）
`install.sh` 会自动生成 `/etc/systemd/system/aiops-backend.service` 并 `enable`：
**服务器重启后平台自动启动，进程异常退出 5 秒后自动拉起。**

```bash
systemctl status  aiops-backend     # 查看运行状态
systemctl restart aiops-backend     # 重启平台
systemctl stop    aiops-backend     # 停止（重启机器后仍会自动启动）
systemctl disable aiops-backend     # 取消开机自启
journalctl -u aiops-backend -f      # 实时日志（等价于 backend/uvicorn.log）
```

> - 若安装环境无 systemd（如容器），脚本自动回退为普通进程方式，改用 `./start.sh` / `./stop.sh` 管理，**重启机器后需手工执行 `./start.sh`**。
> - 手工部署（未执行 install.sh）时，可按上述服务名与路径自行编写 unit 文件，安装目录按实际路径替换。

### 忘记管理员密码（重置 admin）
```bash
cd AIOps
sudo ./deploy/reset_admin.sh                   # 重置为 backend/.env 中的 BOOTSTRAP_ADMIN_PASSWORD
sudo ./deploy/reset_admin.sh 'NewPass@2026'    # 重置为指定密码
sudo ./deploy/reset_admin.sh -u ops 'Pass@2026'  # 重置指定账号（不存在则创建为管理员）
sudo ./deploy/reset_admin.sh -y 'Pass@2026'    # 跳过确认（脚本化调用）
```
> - 脚本直接更新数据库中的 `password_hash`（与平台登录校验同源 bcrypt），**不清库、不删账号**，也**无需安装 psql 客户端**（复用 backend/.venv 的 passlib + psycopg）。
> - 执行后会自动重启 `aiops-backend` 服务使新密码立即生效，并打印使用的账号与密码。
> - 密码含特殊字符时请用单引号包裹；bcrypt 上限 72 字节。

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
| 忘记 admin 密码 / 初始密码登录失败 | 执行 `sudo ./deploy/reset_admin.sh`（见上文「忘记管理员密码」） |
| 重启服务器后平台没自动起来 | 先看 `systemctl status aiops-backend`；若 unit 不存在说明安装时无 systemd，需手工 `./start.sh`，或按上文手工注册服务 |

---
*© 2026 AIOps Platform v4.5.1*
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
