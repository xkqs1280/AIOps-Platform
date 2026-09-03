"""平台授权服务 — 机器指纹 / 激活码验签 / 授权状态 / 到期锁定 / 自动试用 / 时间回拨检测

授权模型（离线激活码，RSA 签名）：
  激活码 = base64url(JSON) + "." + base64url(RSA-SHA256(JSON))
  JSON 字段: {"ver": "trial"|"full", "ed": "YYYY-MM-DD"|"", "fp": 机器指纹, "sn": 序列号}
  - trial 测试版：3 个月（生成时自动计算），到期后锁定平台
  - full  全功能版：永久

自动试用（v4.3.5 起）：
  - 首次部署（license_info 空表）自动激活 3 个月试用，无需厂商参与；
    记录 source='auto'，license_code 标记为 "AUTO-TRIAL"（不走 RSA 签名，本地自产）。
  - 已激活/已录入过授权的存量机器不受影响（表非空不触发）。
  - 时间回拨检测：last_seen_at 锚点只进不退（GREATEST），当前时间比锚点落后
    超过 CLOCK_ROLLBACK_TOLERANCE 判定被回拨 → 临时锁定授权页，校时后自动恢复。

安全：
  - 平台只内置公钥验签，私钥仅厂商持有（tools/generate_license.py）
  - 激活码绑定机器指纹，换机器无效（防复制传播）
  - 签名校验防篡改（改到期日/版本即验签失败）
"""
import base64
import hashlib
import json
import logging
import sys
import time
import uuid
from datetime import datetime, date, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy import select, text

from app.config import settings

logger = logging.getLogger(__name__)

# ---- 内置平台公钥（由厂商 tools/generate_license.py 生成，与 private_key.pem 配对）----
PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAvAK0qyfn0hjkXxGxb5Wa
1Y86MofRQJuDPusrT32aQxrB7p0AHD8BR4XNC0AIY3VVltthO4k9Sb28150BdOje
gYTqrGeMLVxuhLRpX59wEaAYXZz5DMNhboAGdemwP1RvR+AGWUnEXmQbFYD7troO
h7vguyvhhTqV0JYJUKPTs02VKIJgGAeLspIp7v8yNp3basv6efTXusEu+QV5S2F8
ftpXaUNFcuF0S7blC5xoOe1ZtshpDEJJvwU8Ugt8Fbs0EUeyDgXI0/u+vimQDZIT
kL9vKIllOi/DaZEKvm9CMOUrWxilkGWwdQMFom56U2vaeZnJP8qdMa3jabvIICyF
5QIDAQAB
-----END PUBLIC KEY-----
"""

# 授权状态缓存（激活/到期判定以 DB 为准，缓存仅用于减少每请求 DB 查询）
_CACHE: dict = {"ts": 0.0, "status": None}
CACHE_TTL = 60  # 秒

# 自动试用期（天）：首次部署自动激活的试用时长
TRIAL_DAYS = 90
# 时间回拨容差（秒）：当前时间比历史锚点（last_seen_at）落后超过该值 → 判定被回拨
CLOCK_ROLLBACK_TOLERANCE = 24 * 3600


def get_machine_fingerprint() -> str:
    """生成机器指纹（与厂商工具算法一致）：machine-id/MAC + CPU/磁盘序列号 → sha256 前16位。"""
    parts: list[str] = []
    try:
        with open("/etc/machine-id", encoding="utf-8") as f:
            parts.append(f.read().strip())
    except Exception:
        pass
    parts.append(str(uuid.getnode()))
    if sys.platform == "win32":
        import subprocess
        for args in (
            ["wmic", "cpu", "get", "ProcessorId"],
            ["wmic", "diskdrive", "get", "SerialNumber"],
        ):
            try:
                r = subprocess.run(args, capture_output=True, timeout=10)
                for line in r.stdout.decode("gbk", errors="replace").splitlines():
                    line = line.strip()
                    if line and not line.lower().startswith(("processorid", "serialnumber")):
                        parts.append(line)
                        break
            except Exception:
                pass
    return hashlib.sha256("|".join(p for p in parts if p).encode()).hexdigest()[:16].upper()


def _decode_license_code(code: str) -> dict | None:
    """解码并验签激活码，返回 payload；无效/伪造返回 None。
    注意：激活码本身含 base64url 的 `-`/`_`，只能去除空白，不能去 `-`。
    """
    code = code.strip().replace(" ", "").replace("\n", "").replace("\r", "")
    if "." not in code:
        return None
    b64part, sigpart = code.split(".", 1)
    try:
        data = base64.urlsafe_b64decode(b64part + "=" * (-len(b64part) % 4))
        sig = base64.urlsafe_b64decode(sigpart + "=" * (-len(sigpart) % 4))
    except Exception:
        return None
    try:
        public_key = serialization.load_pem_public_key(PUBLIC_KEY_PEM)
        public_key.verify(sig, data, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError):
        return None
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("ver") not in ("trial", "full"):
        return None
    return payload


async def _load_license_row():
    """读取最新授权行，并在同事务内推进时间回拨锚点。

    锚点语义（last_seen_at）：历史观测到的最大服务器时间。
    用 SQL GREATEST(COALESCE(last_seen_at, now), now) 保证只进不退——
    即使本次请求时间被拨回，锚点也不会被拉低，检测持续有效。
    写库频率受外层状态缓存（60s TTL）约束，约每分钟最多一次 UPDATE。
    """
    from app.database import async_session
    from app.models.license import LicenseInfo

    now_dt = datetime.now(timezone.utc)
    async with async_session() as session:
        r = await session.execute(
            select(LicenseInfo).order_by(LicenseInfo.id.desc()).limit(1)
        )
        row = r.scalar_one_or_none()
        if row is not None:
            await session.execute(
                text(
                    "UPDATE license_info SET last_seen_at = "
                    "GREATEST(COALESCE(last_seen_at, :now), :now) WHERE id = :rid"
                ),
                {"now": now_dt, "rid": row.id},
            )
            await session.commit()
            # 重新读取，使内存行携带推进后的锚点（含历史最大值）
            r2 = await session.execute(select(LicenseInfo).where(LicenseInfo.id == row.id))
            row = r2.scalar_one_or_none()
        return row


async def _auto_init_trial() -> bool:
    """首次部署自动激活试用版：license_info 空表时写入一条 3 个月试用记录。

    幂等：写入前复查表是否已空；单进程 uvicorn 下无竞态，
    复查是为了防多 worker / 升级重启瞬间的并发双写。
    """
    from app.database import async_session
    from app.models.license import LicenseInfo

    now_dt = datetime.now(timezone.utc)
    async with async_session() as session:
        r = await session.execute(select(LicenseInfo).limit(1))
        if r.scalar_one_or_none() is not None:
            return False
        row = LicenseInfo(
            version="trial",
            source="auto",
            license_code="AUTO-TRIAL",
            fingerprint=get_machine_fingerprint(),
            activated_at=now_dt,
            expires_at=now_dt + timedelta(days=TRIAL_DAYS),
            last_seen_at=now_dt,
            is_active=True,
        )
        session.add(row)
        await session.commit()
    logger.info(
        "授权模式：首次部署自动激活试用版 %d 天，到期 %s",
        TRIAL_DAYS,
        (now_dt + timedelta(days=TRIAL_DAYS)).isoformat(),
    )
    return True


def _to_utc(dt) -> datetime:
    """naive datetime 视为 UTC，保证比较安全。"""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def get_license_status(db=None):
    """返回当前授权状态（供授权页 / 中间件 / 登录页使用）。

    v4.3.5 行为：
      - LICENSE_ENABLED=False → 不启用授权限制；
      - license_info 空表 → 自动激活 3 个月试用（source='auto'），不锁定；
      - 非永久授权 → 时间回拨检测（被回拨 → 临时锁定，不入缓存，校时自愈）；
      - 试用到期 → 锁定（仅授权页可用），需厂商 full 激活码解锁。
    """
    now = time.time()
    cached = _CACHE.get("status")
    if cached and now - _CACHE["ts"] < CACHE_TTL:
        return cached

    if not settings.LICENSE_ENABLED:
        status = {
            "enabled": False,
            "activated": False,
            "locked": False,
            "version": None,
            "permanent": False,
            "expires_at": None,
            "days_left": None,
            "reason": "未启用授权限制",
            "fingerprint": get_machine_fingerprint(),
        }
        _CACHE["status"], _CACHE["ts"] = status, now
        return status

    # 读 DB（顺带推进回拨锚点）
    license_row = await _load_license_row()

    if license_row is None:
        # ---- 首次部署：自动激活 3 个月试用（空表自动写入，无需厂商参与）----
        try:
            await _auto_init_trial()
        except Exception:
            logger.exception("授权初始化失败：自动激活试用版写入异常")
        license_row = await _load_license_row()
        if license_row is None:
            status = {
                "enabled": True,
                "activated": False,
                "locked": True,
                "version": None,
                "permanent": False,
                "expires_at": None,
                "days_left": None,
                "reason": "授权初始化失败（数据库异常），请检查系统日志",
                "fingerprint": get_machine_fingerprint(),
            }
            _CACHE["status"], _CACHE["ts"] = status, now
            return status

    permanent = license_row.version == "full" or license_row.expires_at is None
    fp = get_machine_fingerprint()
    if permanent:
        status = {
            "enabled": True,
            "activated": True,
            "locked": False,
            "version": license_row.version,
            "permanent": True,
            "expires_at": None,
            "days_left": None,
            "reason": "全功能版（永久）",
            "fingerprint": fp,
        }
        _CACHE["status"], _CACHE["ts"] = status, now
        return status

    # ---- 试用期（非永久）：时间回拨检测 + 到期判定 ----
    expire_dt = _to_utc(license_row.expires_at)
    now_dt = datetime.now(timezone.utc)

    # 时间回拨检测：当前时间比历史锚点落后超过容差 → 判定被回拨
    seen = license_row.last_seen_at
    rolled_back = (
        seen is not None
        and now_dt < _to_utc(seen) - timedelta(seconds=CLOCK_ROLLBACK_TOLERANCE)
    )
    if rolled_back:
        # 临时锁定：不入缓存 → 每次请求实时判定，服务器时间校准后自动恢复
        return {
            "enabled": True,
            "activated": True,
            "locked": True,
            "version": license_row.version,
            "permanent": False,
            "expires_at": expire_dt.isoformat(),
            "days_left": (expire_dt - now_dt).days,
            "reason": "检测到系统时间异常回拨，请校准服务器时间（校准后自动恢复）",
            "fingerprint": fp,
        }

    days_left = (expire_dt - now_dt).days
    locked = expire_dt < now_dt
    reason = (
        "授权已到期，平台已锁定"
        if locked
        else ("试用版（自动激活）" if license_row.source == "auto" else "试用版（3 个月）")
    )
    status = {
        "enabled": True,
        "activated": True,
        "locked": locked,
        "version": license_row.version,
        "permanent": False,
        "expires_at": expire_dt.isoformat(),
        "days_left": days_left,
        "reason": reason,
        "fingerprint": fp,
    }
    _CACHE["status"], _CACHE["ts"] = status, now
    return status


async def activate_license(license_code: str) -> dict:
    """激活：验签 → 指纹比对 → 写入 DB。返回 (ok, message, status)。"""
    from app.database import async_session
    from app.models.license import LicenseInfo

    payload = _decode_license_code(license_code)
    if not payload:
        return {"ok": False, "message": "激活码无效或已被篡改（验签失败）"}

    local_fp = get_machine_fingerprint()
    if payload["fp"].upper() != local_fp:
        return {"ok": False, "message": f"激活码与本机指纹不匹配（本机 {local_fp}）"}

    ver = payload["ver"]
    if ver == "full":
        expires_at = None
    else:
        try:
            expires_at = datetime.strptime(payload["ed"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return {"ok": False, "message": "激活码到期日期无效"}

    now_dt = datetime.now(timezone.utc)
    async with async_session() as session:
        row = LicenseInfo(
            version=ver,
            source="manual",
            license_code=license_code.strip(),
            fingerprint=local_fp,
            activated_at=now_dt,
            expires_at=expires_at,
            last_seen_at=now_dt,
            is_active=True,
        )
        session.add(row)
        await session.commit()

    # 清缓存
    _CACHE["status"], _CACHE["ts"] = None, 0.0
    status = await get_license_status()
    return {"ok": True, "message": "授权激活成功", "status": status}


def clear_license_cache():
    _CACHE["status"], _CACHE["ts"] = None, 0.0


async def is_locked(db=None) -> bool:
    """中间件锁定判定：授权被禁用 / 已激活且有效 → False；未激活或已到期 → True。"""
    status = await get_license_status(db)
    return bool(status.get("locked"))
