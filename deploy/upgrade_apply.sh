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
  sleep 2
  # wait for port 8000 to free
  for i in $(seq 1 15); do
    if ! (ss -tln 2>/dev/null | grep -q ":8000 "); then return 0; fi
    sleep 1
  done
}

start_app() {
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
  [ -d "$AppRoot/backend/app" ] && cp -a "$AppRoot/backend/app" "$bdir/app" 2>/dev/null
  [ -d "$AppRoot/frontend/dist" ] && cp -a "$AppRoot/frontend/dist" "$bdir/frontend" 2>/dev/null
  [ -f "$AppRoot/backend/.env" ] && cp -a "$AppRoot/backend/.env" "$bdir/.env" 2>/dev/null
}

restore_backup() {
  local bdir="$AppRoot/upgrade/backup"
  [ -d "$bdir/app" ] && { rm -rf "$AppRoot/backend/app"; cp -a "$bdir/app" "$AppRoot/backend/app" 2>/dev/null; }
  if [ -d "$bdir/frontend" ]; then
    rm -rf "$AppRoot/frontend/dist"
    mkdir -p "$AppRoot/frontend"
    cp -a "$bdir/frontend" "$AppRoot/frontend/dist" 2>/dev/null
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
write_state "backup" 20 "Stopping service"
stop_app

write_state "backup" 30 "Backing up current files"
backup_existing

write_state "backup" 45 "Dumping database snapshot"
if [ "$SkipDbDump" != "1" ] && dump_database; then
  write_state "backup" 55 "Database snapshot saved"
else
  write_state "backup" 55 "DB dump skipped (pg_dump not found or disabled)"
fi

# ---- Replace (preserve backend/.env) ----
write_state "replacing" 65 "Replacing application files"
FAIL=0
if [ -n "$Staging" ]; then
  if [ -d "$Staging/backend/app" ]; then
    rm -rf "$AppRoot/backend/app"
    mkdir -p "$AppRoot/backend"
    cp -a "$Staging/backend/app" "$AppRoot/backend/app" 2>/dev/null || FAIL=1
  fi
  if [ -d "$Staging/frontend/dist" ]; then
    rm -rf "$AppRoot/frontend/dist"
    mkdir -p "$AppRoot/frontend"
    cp -a "$Staging/frontend/dist" "$AppRoot/frontend/dist" 2>/dev/null || FAIL=1
  fi
  # Windows exe-style package (source deployments ignore the exe)
  if [ -f "$Staging/AIOpsServer.exe" ]; then
    cp -f "$Staging/AIOpsServer.exe" "$AppRoot/AIOpsServer.exe" 2>/dev/null || true
  fi
  # Sync upgrade scripts so the deployed copy stays in sync with this version
  if [ -f "$Staging/deploy/upgrade_apply.sh" ]; then
    mkdir -p "$AppRoot/deploy"
    cp -f "$Staging/deploy/upgrade_apply.sh" "$AppRoot/deploy/upgrade_apply.sh" 2>/dev/null || true
    chmod +x "$AppRoot/deploy/upgrade_apply.sh" 2>/dev/null || true
  fi
fi
if [ "$FAIL" = "1" ]; then
  write_state "failed" 0 "Replace failed, rolling back" "replace failure"
  restore_backup
  start_app
  write_state "rolled_back" 0 "Rolled back after replace failure"
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
