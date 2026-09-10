#!/usr/bin/env bash
# ============================================================
#  AIOps 平台 —— 重置管理员密码（Linux 源码部署）
#
#  用途：忘记 admin 密码，或用 .env 里的初始密码登录失败时，
#        把数据库中的管理员账号密码重置为指定值。
#
#  用法：
#    sudo ./deploy/reset_admin.sh                     # 重置为 backend/.env 中的 BOOTSTRAP_ADMIN_PASSWORD
#    sudo ./deploy/reset_admin.sh 'NewPass@2026'      # 重置为指定密码
#    sudo ./deploy/reset_admin.sh -u ops 'Pass@2026'  # 重置指定账号（不存在则创建为 admin）
#    ./deploy/reset_admin.sh -y 'Pass@2026'           # 跳过确认（脚本化调用）
#    ./deploy/reset_admin.sh -r /opt/AIOps -y 'Pass@2026'   # 显式指定安装目录
#
#  特点：不清库、不删账号，只更新 password_hash（账号不存在才新建）；
#        哈希与平台登录校验同源（passlib + bcrypt），无需安装 psql 客户端。
# ============================================================
set -u

AppRoot="$(cd "$(dirname "$0")/.." && pwd)"
USERNAME=""
PASSWORD=""
ASSUME_YES=0

usage() {
  sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    -u|--user) USERNAME="${2:-}"; shift 2;;
    -r|--root) AppRoot="${2:-}"; shift 2;;
    -y|--yes)  ASSUME_YES=1; shift;;
    -h|--help) usage; exit 0;;
    *) PASSWORD="$1"; shift;;
  esac
done

echo ""
echo "============================================="
echo "   AIOps 重置管理员密码"
echo "============================================="
echo ""

ENV_FILE="$AppRoot/backend/.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "[X] 未找到 $ENV_FILE，请确认安装完整或使用 -r 指定安装目录。"
  exit 1
fi

# 优先使用平台自带 venv（保证 passlib / psycopg 可用）
PY=""
for cand in "$AppRoot/backend/.venv/bin/python3" "$AppRoot/backend/.venv/bin/python" python3; do
  if [ -x "$cand" ]; then PY="$cand"; break; fi
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "[X] 未找到可用的 Python，请确认安装目录下有 backend/.venv。"
  exit 1
fi

if [ "$ASSUME_YES" != "1" ]; then
  printf "确认重置？(y/N) "
  read -r _ans
  case "$_ans" in
    y|Y|yes|YES) ;;
    *) echo "已取消。"; exit 0;;
  esac
fi

echo "  安装目录 : $AppRoot"
echo ""

"$PY" - "$ENV_FILE" "$USERNAME" "$PASSWORD" <<'PYEOF'
import re
import sys

env_path, cli_user, cli_pass = sys.argv[1], sys.argv[2], sys.argv[3]


def parse_env(path):
    data = {}
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            data[key.strip()] = val
    return data


env = parse_env(env_path)
dsn = env.get("DATABASE_URL", "").strip()
username = (cli_user or env.get("BOOTSTRAP_ADMIN_USERNAME") or "admin").strip()
password = cli_pass or env.get("BOOTSTRAP_ADMIN_PASSWORD") or ""

if not dsn:
    print("[X] .env 中未找到 DATABASE_URL。")
    sys.exit(2)
if not password:
    print("[X] 未提供新密码：可在命令后追加密码参数，或在 .env 中配置 BOOTSTRAP_ADMIN_PASSWORD。")
    sys.exit(2)
if len(password.encode("utf-8")) > 72:
    print("[X] 密码超过 72 字节（bcrypt 上限），请换短一些的密码。")
    sys.exit(2)

# SQLAlchemy 方言前缀 -> psycopg 直连串
direct_dsn = re.sub(r"^postgresql\+\w+://", "postgresql://", dsn)

try:
    from passlib.context import CryptContext
except ImportError:
    print("[X] 缺少 passlib，请在安装目录执行：backend/.venv/bin/pip install passlib bcrypt")
    sys.exit(2)

ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
pwd_hash = ctx.hash(password)

try:
    import psycopg
except ImportError:
    print("[X] 缺少 psycopg，请在安装目录执行：backend/.venv/bin/pip install 'psycopg[binary]'")
    sys.exit(2)

try:
    with psycopg.connect(direct_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM users")
            total = cur.fetchone()[0]
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            existed = cur.fetchone() is not None
            if existed:
                cur.execute(
                    "UPDATE users SET password_hash = %s, is_active = true WHERE username = %s",
                    (pwd_hash, username),
                )
            else:
                cur.execute(
                    "INSERT INTO users (username, password_hash, role, is_active)"
                    " VALUES (%s, %s, 'admin', true)",
                    (username, pwd_hash),
                )
        conn.commit()
except Exception as exc:  # noqa: BLE001
    print("[X] 数据库操作失败：%s" % exc)
    print("    请确认 PostgreSQL 正在运行、DATABASE_URL 中的账号密码正确。")
    sys.exit(3)

print("  users 表原账号数 : %d" % total)
print("  目标账号         : %s" % username)
print("  处理方式         : %s" % ("更新已有账号密码" if existed else "新建管理员账号"))
print("")
print("[OK] 密码已重置为 : %s" % password)
print("     请用该账号登录平台验证（建议登录后立即在「个人设置」中修改）。")
PYEOF

RC=$?
echo ""
if [ "$RC" != "0" ]; then
  echo "[X] 重置失败（错误码 $RC）。"
  exit "$RC"
fi

# ---------- 重启服务，使新密码立即生效 ----------
SVC="aiops-backend"
if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
if command -v systemctl >/dev/null 2>&1 && [ -f "/etc/systemd/system/${SVC}.service" ]; then
  echo "重启服务 ${SVC} …"
  if $SUDO systemctl restart "$SVC" >/dev/null 2>&1; then
    sleep 3
    if $SUDO systemctl is-active --quiet "$SVC"; then
      echo "[OK] 服务已重启（systemctl status ${SVC} 查看状态）"
    else
      echo "[!] 服务未处于 active，请执行：journalctl -u ${SVC} -n 50 --no-pager"
    fi
  else
    echo "[!] 重启失败，请手动执行：${SUDO} systemctl restart ${SVC}"
  fi
else
  echo "[i] 未检测到 systemd 服务；若平台以裸进程运行，请执行 ./stop.sh && ./start.sh"
fi
echo ""
