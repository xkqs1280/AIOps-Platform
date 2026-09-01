# -*- coding: utf-8 -*-
"""一键升级服务：升级包上传 / 校验 / 状态机 / 执行 / 回滚

升级流程（状态机，状态持久化到 upgrade/state.json，服务重启后仍可查询）：
    idle → uploading → validating → backup → applying → replacing → restarting
    → verifying → done
    任一步失败 → failed（自动回滚到 backup/，可手动重试）

升级包格式（zip）：
    manifest.json      版本/构建时间/变更说明/最低兼容版本/签名
    AIOpsServer.exe    新版本后端程序（可选，源码模式不需要）
    frontend/dist/*    新前端产物

数据保护：
    1. PostgreSQL 数据独立于应用目录，升级不触碰 → 设备/告警/配置天然保留
    2. backend/.env 升级前自动备份，替换时跳过 → CREDENTIAL_ENCRYPTION_KEY 保留，
       设备凭据可正常解密
    3. 升级前 pg_dump 全库快照到 backup/db_<version>_<ts>.sql（尽力而为）
    4. 升级失败自动回滚：恢复 exe + frontend/dist + .env

执行方式：后端 spawn 独立的 PowerShell 脚本 upgrade_apply.ps1（DETACHED_PROCESS），
由它执行「停服 → 备份 → 替换 → 重启 → 健康检查」，后端进程本身会被停掉。
"""
import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.config import BACKEND_DIR
from app.services.license_service import PUBLIC_KEY_PEM
from app.version import APP_VERSION, version_gt, version_ge

logger = logging.getLogger(__name__)

# 允许的最小打包版本（低于此版本的升级包拒绝，防止旧包覆盖新平台）
MIN_SUPPORTED_VERSION = "3.6.0"

# 状态机取值
STATE_IDLE = "idle"
STATE_UPLOADING = "uploading"
STATE_VALIDATING = "validating"
STATE_BACKUP = "backup"
STATE_APPLYING = "applying"
STATE_REPLACING = "replacing"
STATE_RESTARTING = "restarting"
STATE_VERIFYING = "verifying"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_ROLLED_BACK = "rolled_back"


def get_app_root() -> Path:
    """应用根目录：frozen → exe 同级；源码模式 → backend 上跳 1 级。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return BACKEND_DIR.parent


def get_upgrade_root() -> Path:
    """升级工作目录（frozen: exe 同级 upgrade/；源码: 项目根 upgrade/）。"""
    return get_app_root() / "upgrade"


def _state_file() -> Path:
    return get_upgrade_root() / "state.json"


def load_state() -> dict:
    """读取升级状态文件（不存在 → 默认 idle 状态）。"""
    default = {
        "state": STATE_IDLE,
        "progress": 0,
        "message": "",
        "from_version": APP_VERSION,
        "to_version": None,
        "started_at": None,
        "finished_at": None,
        "error": None,
        "rollback_available": False,
        "log": [],
    }
    try:
        # utf-8-sig：兼容 PowerShell Out-File 写入的带 BOM 状态文件
        data = json.loads(_state_file().read_text(encoding="utf-8-sig"))
        # 防御：历史上旧版 ps1 可能把状态文件写坏成字符串/数组
        if not isinstance(data, dict):
            return default
        merged = {**default, **data}
        merged.setdefault("log", [])
        return merged
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return default


def save_state(**updates) -> dict:
    """原子写入状态文件（tmp + rename，避免半写）。"""
    root = get_upgrade_root()
    root.mkdir(parents=True, exist_ok=True)
    state = load_state()
    state.update(updates)
    if isinstance(state.get("log"), list):
        # 日志只保留最近 200 条，避免无限膨胀
        state["log"] = state["log"][-200:]
    tmp = root / "state.json.tmp"
    # utf-8-sig（带 BOM）：确保 PowerShell 5.1 Get-Content 默认编码也能正确读中文日志
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8-sig")
    tmp.replace(_state_file())
    return state


def log_state(message: str) -> dict:
    """追加一条日志并返回最新状态。"""
    state = load_state()
    log = state.setdefault("log", [])
    log.append(f"[{time.strftime('%H:%M:%S')}] {message}")
    return save_state(log=log)


def get_status() -> dict:
    """对外暴露的状态（脱敏：去掉内部字段）。"""
    s = load_state()
    return {
        "state": s["state"],
        "progress": s["progress"],
        "message": s["message"],
        "from_version": s["from_version"],
        "to_version": s["to_version"],
        "started_at": s["started_at"],
        "finished_at": s["finished_at"],
        "error": s["error"],
        "rollback_available": bool(s["rollback_available"]),
        "log": s["log"],
    }


def _normalize_manifest(manifest: dict) -> bytes:
    """规范化 manifest（剔除签名字段、按 key 排序、紧凑 JSON）用于验签/签名。"""
    body = {k: v for k, v in manifest.items() if k != "signature"}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_signature(manifest: dict) -> bool:
    """校验 manifest.json 的 RSA-SHA256 签名（复用平台公钥）。"""
    sig_b64 = manifest.get("signature")
    if not sig_b64:
        return False
    try:
        sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
        public_key = serialization.load_pem_public_key(PUBLIC_KEY_PEM)
        public_key.verify(sig, _normalize_manifest(manifest), padding.PKCS1v15(), hashes.SHA256())
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return False


def _verify_file_hashes(staging: Path, manifest: dict) -> None:
    """校验升级包内文件与 manifest 的 sha256 清单完全一致（全文件完整性校验）。

    背景：签名只覆盖 manifest.json，此前包内 AIOpsServer.exe / frontend/dist /
    upgrade_apply.ps1 / backend/app 等落地文件不在签名覆盖范围，攻击者或供应链
    篡改包内文件（保持 manifest 不变）即可通过校验，升级脚本以系统权限执行
    → RCE + 持久化。修复：manifest 携带 files = {相对路径: sha256}（该清单已
    随 manifest 一起被签名保护），此处逐文件比对，任何不匹配或清单外文件一律拒绝。
    """
    import hashlib

    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("升级包缺少文件完整性清单（files），请使用 4.3.4+ 打包工具重新打包")

    for rel, expect in files.items():
        p = staging / rel
        if not p.is_file():
            raise ValueError(f"升级包缺少清单内文件: {rel}")
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        if h != str(expect).lower():
            raise ValueError(f"升级包文件校验失败（可能被篡改）: {rel}")

    # 反向校验：包内所有文件必须都在清单内，防夹带未授权文件随升级落地
    allowed = set(files) | {"manifest.json"}
    for p in sorted(staging.rglob("*")):
        if p.is_file():
            rel = p.relative_to(staging).as_posix()
            if rel not in allowed:
                raise ValueError(f"升级包包含未列入清单的文件: {rel}")


def validate_package(zip_path: Path) -> dict:
    """校验升级包并解压到 staging，返回 manifest；失败抛异常（message 为原因）。"""
    root = get_upgrade_root()
    staging = root / "staging"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        # zip-slip 防护：拒绝路径穿越（is_relative_to 严格判断，避免前缀字符串绕过）
        for name in zf.namelist():
            resolved = (staging / name).resolve()
            if not resolved.is_relative_to(staging.resolve()):
                raise ValueError("升级包包含非法路径")
        zf.extractall(staging)

    manifest_file = staging / "manifest.json"
    if not manifest_file.is_file():
        raise ValueError("升级包缺少 manifest.json")

    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if not verify_signature(manifest):
        raise ValueError("升级包签名校验失败（包可能被篡改或非官方包）")

    # 全文件 sha256 完整性校验：签名覆盖 files 清单，清单覆盖所有落地文件
    _verify_file_hashes(staging, manifest)

    ver = str(manifest.get("version", "")).strip()
    if not ver:
        raise ValueError("manifest.json 缺少 version 字段")
    if not version_gt(ver, APP_VERSION):
        raise ValueError(f"升级包版本 {ver} 不高于当前版本 {APP_VERSION}，无法升级")
    min_sup = str(manifest.get("min_supported_version", MIN_SUPPORTED_VERSION)).strip() or MIN_SUPPORTED_VERSION
    if not version_ge(APP_VERSION, min_sup):
        raise ValueError(f"当前版本 {APP_VERSION} 低于升级包要求的最低版本 {min_sup}，请先升级到中间版本")

    manifest["_staging"] = str(staging)
    manifest["_resolved_version"] = ver
    return manifest


def _apply_script() -> tuple[Path, list[str]]:
    """按平台返回升级执行脚本路径与启动前缀。

    Windows：deploy/upgrade_apply.ps1（powershell.exe -File）
    Linux：  deploy/upgrade_apply.sh（bash）
    返回 (script_path, prefix_cmd)。
    """
    app_root = get_app_root()
    if sys.platform == "win32":
        name = "upgrade_apply.ps1"
        prefix = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]
    else:
        name = "upgrade_apply.sh"
        prefix = ["bash"]
    candidates = [
        # 升级包 staging 内的脚本优先（脚本随版本演进，避免使用当前部署目录的旧脚本）
        get_upgrade_root() / "staging" / "deploy" / name,
        app_root / "deploy" / name,
        app_root / name,
        BACKEND_DIR.parent.parent / "deploy" / name,  # 源码模式
    ]
    for c in candidates:
        if c.is_file():
            return c, prefix
    return candidates[1], prefix


def _spawn_detached(cmd: list[str], cwd: Path) -> None:
    """spawn 独立脚本进程（后端进程被停掉后脚本继续执行）。

    关键坑：Windows 上不能使用 DETACHED_PROCESS（0x8）——实测该标志会导致
    PowerShell 子进程启动后立即失败，脚本从不执行。改用 CREATE_NEW_PROCESS_GROUP
    (0x200) + CREATE_NO_WINDOW(0x08000000)：已验证脚本正常执行，且父进程被
    taskkill 强杀后子进程仍独立存活。

    脚本 stdout/stderr 重定向到 upgrade/apply.log，便于排查脚本启动失败原因。
    """
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    log_path = get_upgrade_root() / "apply.log"
    log_handle = open(log_path, "ab", buffering=0)
    subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=log_handle,
        stderr=log_handle,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )


def start_upgrade(zip_path: Path) -> dict:
    """开始升级：校验 → 备份标记 → spawn 独立升级脚本。"""
    manifest = validate_package(zip_path)
    staging = Path(manifest["_staging"])
    to_version = manifest["_resolved_version"]

    save_state(
        state=STATE_UPLOADING,
        progress=10,
        message="升级包校验通过，准备升级",
        to_version=to_version,
        started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        finished_at=None,
        error=None,
        rollback_available=False,
        log=[],
    )
    log_state(f"升级包校验通过：{APP_VERSION} → {to_version}")

    script, prefix = _apply_script()
    if not script.is_file():
        save_state(state=STATE_FAILED, message=f"未找到升级执行脚本 {script.name}", error=f"{script.name} 不存在")
        raise FileNotFoundError(f"{script.name} 不存在")

    # 生成升级脚本的调用参数（幂等可重试：脚本内部支持断点续做/失败回滚）
    apply_args = {
        "AppRoot": str(get_app_root()),
        "Staging": str(staging),
        "FromVersion": APP_VERSION,
        "ToVersion": to_version,
        "StateFile": str(_state_file()),
        "SkipDbDump": "1" if os.environ.get("AIOPS_UPGRADE_SKIP_DB") else "0",
        # Windows 源码部署时用它重启 python 后端（生产 exe 部署忽略）
        "PythonPath": sys.executable,
    }
    cmd = list(prefix) + [str(script)]
    for k, v in apply_args.items():
        cmd += ["-" + k, v]

    try:
        _spawn_detached(cmd, get_app_root())
    except Exception as e:
        save_state(state=STATE_FAILED, message=f"启动升级脚本失败：{e}", error=str(e))
        raise

    log_state("升级脚本已启动（后台执行）")
    return get_status()


_ACTIVE_STATES = (STATE_UPLOADING, STATE_VALIDATING, STATE_BACKUP, STATE_APPLYING,
                  STATE_REPLACING, STATE_RESTARTING, STATE_VERIFYING)
# 升级中间态超过该秒数仍未完成，视为"僵尸状态"（进程已死/中断），自动重置解锁
_ZOMBIE_TIMEOUT_SECONDS = 300


def _is_zombie_state(s: dict) -> bool:
    """判断当前中间态是否为僵尸（升级脚本早已退出，状态被遗留）。"""
    if s["state"] not in _ACTIVE_STATES:
        return False
    started = s.get("started_at")
    if not started:
        return True
    try:
        from datetime import datetime
        started_dt = datetime.strptime(str(started)[:19], "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - started_dt).total_seconds() > _ZOMBIE_TIMEOUT_SECONDS
    except Exception:
        return True


def can_upgrade() -> tuple:
    """是否可发起新升级（避免并发）。返回 (ok, reason)。"""
    s = load_state()
    if s["state"] in _ACTIVE_STATES:
        if _is_zombie_state(s):
            # 僵尸状态：升级进程已退出但状态文件遗留，自动重置解锁
            save_state(
                state=STATE_IDLE, progress=0,
                message="检测到上次升级中断，状态已自动重置，可重新上传",
                started_at=None, finished_at=None, error=None, log=[],
            )
            return True, ""
        return False, "已有升级正在进行，请等待完成"
    return True, ""


def request_rollback() -> dict:
    """手动回滚：将 backup/ 恢复并重启。返回最新状态。"""
    root = get_upgrade_root()
    backup_dir = root / "backup"
    if not backup_dir.exists():
        save_state(state=STATE_FAILED, message="无可回滚的备份", error="backup 目录不存在")
        raise FileNotFoundError("backup 目录不存在")

    save_state(state=STATE_ROLLED_BACK, progress=5, message="开始回滚", error=None)
    log_state("用户请求回滚，启动回滚脚本")

    script, prefix = _apply_script()
    cmd = list(prefix) + [str(script), "-Rollback", "1",
                          "-AppRoot", str(get_app_root()),
                          "-StateFile", str(_state_file()),
                          "-PythonPath", sys.executable]
    try:
        _spawn_detached(cmd, get_app_root())
    except Exception as e:
        save_state(state=STATE_FAILED, message=f"启动回滚脚本失败：{e}", error=str(e))
        raise
    return get_status()


def save_uploaded_zip(upload_path: Path) -> Path:
    """保存上传的升级包到 upgrade/incoming/，返回最终路径。"""
    root = get_upgrade_root()
    incoming = root / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    dest = incoming / f"upgrade_{int(time.time())}.zip"
    shutil.copy2(upload_path, dest)
    return dest
