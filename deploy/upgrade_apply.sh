#!/usr/bin/env bash
# AIOps One-click Upgrade Apply Script (Linux, source deployment)
# Mirrors deploy/upgrade_apply.ps1 logic for Linux source deployments:
#   stop -> backup (incl. .env + pg_dump) -> replace (skip .env) -> start -> health check -> done
# Rollback mode: restore from upgrade/backup and restart.
#
# Invoked detached by upgrade_service.py; state JSON written via venv python
# (utf-8-sig to stay compatible with the Windows PowerShell writer).
set -u

AppRoot=""
Staging=""
StateFile=""
SkipDbDump="0"
ROLLBACK=0

while [ $# -gt 0 ]; do
  case "$1" in
    -AppRoot) AppRoot="$2"; shift 2;;
    -Staging) Staging="$2"; shift 2;;
    -StateFile) StateFile="$2"; shift 2;;
    -SkipDbDump) SkipDbDump="$2"; shift 2;;
    -Rollback) ROLLBACK=1; shift;;
    *) shift;;
  esac
done

# ===============================================================
# 0) 脱离 aiops-backend.service 的 cgroup —— 修复"升级卡在 90%"
#
# 本脚本由后端子进程 spawn，与 uvicorn 同属 systemd unit aiops-backend.service
# 的 cgroup（KillMode=control-group）。脚本 pkill 停服后，systemd 按 Restart=always
# 在 RestartSec 后重启服务，而重启的 stop 阶段会清空整个 cgroup —— 把正在做健康
# 检查的本脚本一起杀掉。没人写 done，状态便永久停在 verifying/90%，
# 页面表现为"升级卡住"（实测 .108 上 4.5.6→4.5.7 即如此）。
#
# 解法：用 systemd-run 把自身搬进一个独立 scope。unit 以 root 运行时用系统级
# scope；以普通用户运行时用该用户的 systemd --user 实例（无需 sudo 授权）。
# 环境不具备时不阻断升级 —— 原地继续，由后端启动自愈
# （upgrade_service.reconcile_interrupted_upgrade）补写终态兜底。
# ===============================================================
SELF="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/$(basename "$0")"

# 本进程是否仍被困在 aiops-backend.service 的 cgroup 内（幂等判据，避免重复脱离）
in_backend_cgroup() { grep -qs 'aiops-backend\.service' /proc/self/cgroup 2>/dev/null; }

# 服务是否由 systemd 托管（只读探测，不需要授权）
has_systemd_unit() {
  [ -d /run/systemd/system ] || return 1
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl show aiops-backend -p LoadState 2>/dev/null | grep -q 'LoadState=loaded'
}

# 把自身重新拉起在一个独立 scope 里。成功接管返回 0（调用方须立刻 exit），
# 环境不支持则返回 1（调用方就地继续）。
detach_from_cgroup() {
  [ -d /run/systemd/system ] || return 1
  command -v systemd-run >/dev/null 2>&1 || return 1
  [ -r "$SELF" ] || return 1
  local unit="aiops-upgrade-$$" child="" i
  if [ "$(id -u 2>/dev/null)" = "0" ]; then
    systemd-run --scope --collect --unit="$unit" \
      --setenv=AIOPS_UPGRADE_DETACHED=1 -- "$SELF" "$@" </dev/null &
    child=$!
  else
    # 普通用户：走自己的 systemd --user 实例。注意 systemd 系统级 unit
    # 不带 PAM 会话环境，XDG_RUNTIME_DIR / DBUS 地址需要显式补上。
    : "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
    export XDG_RUNTIME_DIR
    # 让 user manager 常驻：否则它会随管理员最后一个 SSH 会话退出而停止，
    # 连带清掉本脚本所在的 scope（实测正是这样"跑到一半被杀、状态卡 90%"
    # —— 会话在则成功、会话断则失败，行为不可预测）。无权限时静默跳过，
    # 后面仍有后端启动自愈兜底。
    command -v loginctl >/dev/null 2>&1 && loginctl enable-linger "$(id -un)" >/dev/null 2>&1
    # 等 bus 就绪（enable-linger 首次会拉起 user@.service，需要一点时间）
    for i in $(seq 1 10); do
      [ -S "$XDG_RUNTIME_DIR/bus" ] && break
      sleep 0.5
    done
    [ -S "$XDG_RUNTIME_DIR/bus" ] || return 1
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
    systemd-run --user --scope --collect --unit="$unit" \
      --setenv=AIOPS_UPGRADE_DETACHED=1 -- "$SELF" "$@" </dev/null &
    child=$!
  fi
  # systemd-run 在 --scope 模式下会一直守着子进程。它仍存活 => scope 已建立、
  # 升级流程已由子进程接管；已退出 => 启动失败，回退到当前 cgroup 继续。
  sleep 2
  kill -0 "$child" 2>/dev/null && return 0
  return 1
}

if in_backend_cgroup && detach_from_cgroup "$@"; then
  echo "[i] detached from aiops-backend.service cgroup (running in an independent scope)"
  exit 0
fi

[ -z "$AppRoot" ] && AppRoot="$(cd "$(dirname "$0")/.." && pwd)"
[ -z "$StateFile" ] && StateFile="$AppRoot/upgrade/state.json"

# python that can write JSON (utf-8-sig) -- prefer the backend venv
PY=""
for cand in "$AppRoot/backend/.venv/bin/python3" "$AppRoot/backend/.venv/bin/python" /usr/bin/python3 python3; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
[ -z "$PY" ] && PY="python3"

write_state() {
  local state="$1" prog="$2" msg="$3" err="${4:-}"
  "$PY" - "$StateFile" "$state" "$prog" "$msg" "$err" <<'PYEOF'
import json, sys, time, os
path, state, prog, msg, err = sys.argv[1:6]
data = {"state": state, "progress": int(prog), "message": msg, "error": err}
if os.path.exists(path):
    try:
        old = json.load(open(path, encoding="utf-8-sig"))
        for k in ("from_version", "to_version", "started_at", "log"):
            if k in old:
                data[k] = old[k]
    except Exception:
        pass
data.setdefault("log", [])
data["log"].append("[%s] %s" % (time.strftime("%H:%M:%S"), msg))
data["log"] = data["log"][-200:]
os.makedirs(os.path.dirname(path), exist_ok=True)
# utf-8-sig (BOM) keeps compatibility with the Windows PowerShell writer
with open(path, "w", encoding="utf-8-sig") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
PYEOF
}

log_state() { write_state "${1:-processing}" "${2:-0}" "$3" "${4:-}"; }

find_pg_dump() {
  for p in $(command -v pg_dump 2>/dev/null) /usr/bin/pg_dump /usr/lib/postgresql/*/bin/pg_dump; do
    [ -x "$p" ] && echo "$p" && return 0
  done
  return 1
}

read_db_url() {
  local envf="$AppRoot/backend/.env"
  [ -f "$envf" ] || envf="$AppRoot/.env"
  [ -f "$envf" ] || { echo ""; return; }
  grep -E "^DATABASE_URL=" "$envf" | head -1 | cut -d= -f2- | tr -d ' \r'
}

stop_app() {
  pkill -f "uvicorn app.main:app" 2>/dev/null
  pkill -f "uvicorn aiops" 2>/dev/null
  # 只等进程退出，不再等"8000 端口空闲"：systemd 托管时 pkill 被判为异常退出，
  # Restart=always 会在 RestartSec(=5s) 后自动拉起服务并重新占用 8000 —— 等端口
  # 只会一直等到超时。文件替换已改为 mv 原子切换，不依赖端口空闲。
  sleep 2
  return 0
}

start_app() {
  # systemd 托管时不要另起裸进程：pkill 之后 Restart=always 会自动拉起服务（只读
  # 查询即可确认，不需要 systemctl 授权）。若此时再 nohup 起一个，会与 systemd
  # 争抢 8000 端口并留下游离进程。
  if has_systemd_unit; then
    for i in $(seq 1 20); do
      [ "$(systemctl is-active aiops-backend 2>/dev/null)" = "active" ] && return 0
      sleep 1
    done
  fi
  local certs="$AppRoot/backend/certs"
  local ssl_args=()
  if [ -f "$certs/server.key" ] && [ -f "$certs/server.crt" ]; then
    ssl_args=(--ssl-keyfile "$certs/server.key" --ssl-certfile "$certs/server.crt")
  fi
  local vpython="$AppRoot/backend/.venv/bin/python"
  local uv="$AppRoot/backend/.venv/bin/uvicorn"
  if [ ! -x "$uv" ]; then
    # fallback: run uvicorn module via venv python
    (cd "$AppRoot/backend" && nohup "$vpython" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 "${ssl_args[@]}" >> "$AppRoot/deploy/backend-linux.log" 2>&1 &)
    return
  fi
  (cd "$AppRoot/backend" && nohup "$uv" app.main:app --host 0.0.0.0 --port 8000 "${ssl_args[@]}" >> "$AppRoot/deploy/backend-linux.log" 2>&1 &)
}

wait_healthy() {
  local url="http://127.0.0.1:8000/health"
  if [ -f "$AppRoot/backend/certs/server.crt" ]; then url="https://127.0.0.1:8000/health"; fi
  # 放宽等待窗口：冷启动（PyInstaller onefile 解压 + 杀软/慢磁盘 + uvicorn 引导）
  # 可能超过 2 分钟，原来 40 次 (约 2 分钟) 会在替换成功后误报 health check timeout。
  for i in $(seq 1 150); do
    sleep 3
    if curl -sk -m 8 "$url" >/dev/null 2>&1; then return 0; fi
  done
  return 1
}

backup_existing() {
  local bdir="$AppRoot/upgrade/backup"
  mkdir -p "$bdir"
  # 关键：cp -a SRC DST 在 DST 已存在时的语义是"复制进 DST 里面"，而不是覆盖它。
  # 不先清目标，备份就会随每次升级越滚越深，实测 .108 上是三层：
  #   backup/app/version.py       -> 4.0.0  (8月21日，最外层)
  #   backup/app/app/version.py   -> 4.5.6
  #   backup/app/app/app/version.py -> 4.5.1
  # 而 restore_backup 恢复的正是最外层 —— 也就是"回滚"会把系统滚回几周前的旧版本。
  # （Windows 侧 ps1 的 Copy-Item 前已有 Remove-Item，所以该缺陷只在 Linux 侧。）
  if [ -d "$AppRoot/backend/app" ]; then
    rm -rf "$bdir/app"
    cp -a "$AppRoot/backend/app" "$bdir/app" 2>/dev/null
  fi
  if [ -d "$AppRoot/frontend/dist" ]; then
    rm -rf "$bdir/frontend"
    cp -a "$AppRoot/frontend/dist" "$bdir/frontend" 2>/dev/null
  fi
  # .env 是单文件，cp 的覆盖语义本就正确，无需预清理
  [ -f "$AppRoot/backend/.env" ] && cp -a "$AppRoot/backend/.env" "$bdir/.env" 2>/dev/null
}

restore_backup() {
  local bdir="$AppRoot/upgrade/backup"
  # 同样用"预置 + mv 原子切换"：回滚时 systemd 也会在 RestartSec 后拉起服务，
  # 若边删边拷会出现"代码目录不存在"的空缺窗口。
  if [ -d "$bdir/app" ]; then
    rm -rf "$AppRoot/backend/app.rb.new"
    mkdir -p "$AppRoot/backend"
    cp -a "$bdir/app" "$AppRoot/backend/app.rb.new" 2>/dev/null
    if [ -d "$AppRoot/backend/app.rb.new" ]; then
      rm -rf "$AppRoot/backend/app.rb.old"
      [ -d "$AppRoot/backend/app" ] && mv "$AppRoot/backend/app" "$AppRoot/backend/app.rb.old" 2>/dev/null
      mv "$AppRoot/backend/app.rb.new" "$AppRoot/backend/app" 2>/dev/null
    fi
  fi
  if [ -d "$bdir/frontend" ]; then
    rm -rf "$AppRoot/frontend/dist.rb.new"
    mkdir -p "$AppRoot/frontend"
    cp -a "$bdir/frontend" "$AppRoot/frontend/dist.rb.new" 2>/dev/null
    if [ -d "$AppRoot/frontend/dist.rb.new" ]; then
      rm -rf "$AppRoot/frontend/dist.rb.old"
      [ -d "$AppRoot/frontend/dist" ] && mv "$AppRoot/frontend/dist" "$AppRoot/frontend/dist.rb.old" 2>/dev/null
      mv "$AppRoot/frontend/dist.rb.new" "$AppRoot/frontend/dist" 2>/dev/null
    fi
  fi
  [ -f "$bdir/.env" ] && cp -a "$bdir/.env" "$AppRoot/backend/.env" 2>/dev/null
}

dump_database() {
  local pgdump="$(find_pg_dump)"
  [ -n "$pgdump" ] || return 1
  local url="$(read_db_url)"
  [ -n "$url" ] || return 1
  local m
  m="$(echo "$url" | sed -nE 's#^postgresql(\+psycopg_async)?://([^:]+):([^@]+)@([^:]+):([0-9]+)/([A-Za-z0-9_]+)$#\2 \3 \4 \5 \6#p')"
  [ -n "$m" ] || return 1
  local dbUser dbPass dbHost dbPort dbName
  dbUser="$(echo "$m" | awk '{print $1}')"
  dbPass="$(echo "$m" | awk '{print $2}')"
  dbHost="$(echo "$m" | awk '{print $3}')"
  dbPort="$(echo "$m" | awk '{print $4}')"
  dbName="$(echo "$m" | awk '{print $5}')"
  local bdir="$AppRoot/upgrade/backup"
  mkdir -p "$bdir"
  local out="$bdir/db_${dbName}_$(date +%Y%m%d_%H%M%S).sql"
  PGPASSWORD="$dbPass" "$pgdump" -U "$dbUser" -h "$dbHost" -p "$dbPort" -d "$dbName" -F c -f "$out" >/dev/null 2>&1
  if [ $? -eq 0 ] && [ -f "$out" ]; then return 0; fi
  return 1
}

# ============================================================
echo "AIOps upgrade/rollback script (Linux). AppRoot=$AppRoot"

if [ "$ROLLBACK" = "1" ]; then
  write_state "rolled_back" 10 "Rollback started"
  local_bdir="$AppRoot/upgrade/backup"
  if [ ! -d "$local_bdir" ]; then
    write_state "failed" 0 "No backup available" "backup dir missing"
    exit 1
  fi
  stop_app
  write_state "rolled_back" 40 "Service stopped, restoring backup"
  restore_backup
  write_state "rolled_back" 70 "Backup restored, starting service"
  start_app
  if wait_healthy; then
    write_state "rolled_back" 100 "Rollback completed, service healthy"
  else
    write_state "failed" 0 "Rollback completed but health check failed" "health check timeout"
  fi
  exit 0
fi

# ---- Upgrade flow ----
# 顺序说明：备份 / dump / 预置新文件都在"服务仍在运行"时完成（只读，或只写新增的
# *.new 路径，不动在用文件）；停服之后只用 mv 做原子切换（毫秒级）。原因见文件头
# 0 节：pkill 之后 systemd 会在 RestartSec(=5s) 自动拉起服务，若那时才做
# rm -rf + cp -a（数秒窗口），新起来的进程会撞上"代码目录被删"的空缺状态。
write_state "backup" 20 "Backing up current files"
backup_existing

write_state "backup" 45 "Dumping database snapshot"
if [ "$SkipDbDump" != "1" ] && dump_database; then
  write_state "backup" 55 "Database snapshot saved"
else
  write_state "backup" 55 "DB dump skipped (pg_dump not found or disabled)"
fi

# ---- Stage new files (preserve backend/.env: it is never staged nor overwritten) ----
write_state "replacing" 60 "Staging new application files"
FAIL=0
NEW_APP="$AppRoot/backend/app.new"
NEW_DIST="$AppRoot/frontend/dist.new"
if [ -n "$Staging" ]; then
  if [ -d "$Staging/backend/app" ]; then
    rm -rf "$NEW_APP"
    mkdir -p "$AppRoot/backend"
    cp -a "$Staging/backend/app" "$NEW_APP" 2>/dev/null || FAIL=1
  fi
  if [ -d "$Staging/frontend/dist" ]; then
    rm -rf "$NEW_DIST"
    mkdir -p "$AppRoot/frontend"
    cp -a "$Staging/frontend/dist" "$NEW_DIST" 2>/dev/null || FAIL=1
  fi
  # Sync upgrade scripts so the deployed copy stays in sync with this version
  if [ -f "$Staging/deploy/upgrade_apply.sh" ]; then
    mkdir -p "$AppRoot/deploy"
    cp -f "$Staging/deploy/upgrade_apply.sh" "$AppRoot/deploy/upgrade_apply.sh" 2>/dev/null || true
    chmod +x "$AppRoot/deploy/upgrade_apply.sh" 2>/dev/null || true
  fi
  # Linux 源码部署用不到 Windows 产物：旧版本会把 AIOpsServer.exe 复制进安装目录
  # （白占约 100MB 且永远执行不到），这里改为反向清理，并回收 staging 内的副本。
  rm -f "$AppRoot/AIOpsServer.exe" "$AppRoot/AIOpsService.exe" 2>/dev/null || true
  rm -f "$Staging/AIOpsServer.exe" "$Staging/AIOpsService.exe" 2>/dev/null || true
fi
if [ "$FAIL" = "1" ]; then
  write_state "failed" 0 "Staging new files failed" "copy failure"
  rm -rf "$NEW_APP" "$NEW_DIST" 2>/dev/null
  exit 1
fi

# ---- Stop service, then switch atomically ----
write_state "replacing" 68 "Stopping service"
stop_app

write_state "replacing" 72 "Switching to new version"
if [ -d "$NEW_APP" ]; then
  rm -rf "$AppRoot/backend/app.old"
  [ -d "$AppRoot/backend/app" ] && mv "$AppRoot/backend/app" "$AppRoot/backend/app.old" 2>/dev/null
  mv "$NEW_APP" "$AppRoot/backend/app" 2>/dev/null || FAIL=1
fi
if [ -d "$NEW_DIST" ]; then
  rm -rf "$AppRoot/frontend/dist.old"
  [ -d "$AppRoot/frontend/dist" ] && mv "$AppRoot/frontend/dist" "$AppRoot/frontend/dist.old" 2>/dev/null
  mv "$NEW_DIST" "$AppRoot/frontend/dist" 2>/dev/null || FAIL=1
fi
if [ "$FAIL" = "1" ]; then
  write_state "failed" 0 "Switch failed, rolling back" "switch failure"
  restore_backup
  start_app
  write_state "rolled_back" 0 "Rolled back after switch failure"
  exit 1
fi

write_state "restarting" 80 "Starting new version"
start_app

write_state "verifying" 90 "Waiting for service health"
if wait_healthy; then
  write_state "done" 100 "Upgrade completed successfully"
  echo "Upgrade done."
  exit 0
else
  write_state "failed" 0 "Health check failed after restart" "health check timeout"
  exit 1
fi
