"""Encryption helpers for device access credentials.

Set CREDENTIAL_ENCRYPTION_KEY to a Fernet key in backend/.env. Existing
plaintext values stay readable during migration but every new/updated secret
is encrypted once the key is configured.
"""
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

PREFIX = "enc:"


def _cipher() -> Fernet | None:
    """返回 Fernet 加密器；key 缺失或无效时返回 None（降级为明文，避免接口 500）。

    历史上曾出现过生成 43 字符无 padding 的无效 key，导致 protect_secret 抛
    ValueError 使设备创建接口 500。这里对无效 key 静默降级：凭据以明文存储，
    功能可用，管理员可稍后修正 .env 中的 CREDENTIAL_ENCRYPTION_KEY。
    """
    key = settings.CREDENTIAL_ENCRYPTION_KEY.strip()
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except Exception:
        return None


def protect_secret(value: str | None) -> str | None:
    if not value or value.startswith(PREFIX):
        return value
    cipher = _cipher()
    return PREFIX + cipher.encrypt(value.encode()).decode() if cipher else value


def reveal_secret(value: str | None) -> str | None:
    if not value or not value.startswith(PREFIX):
        return value
    cipher = _cipher()
    if not cipher:
        raise RuntimeError("凭据已加密，但未配置 CREDENTIAL_ENCRYPTION_KEY")
    try:
        return cipher.decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken as error:
        raise RuntimeError("设备凭据无法解密") from error


def protect_device_secrets(data: dict) -> dict:
    result = data.copy()
    for field in ("snmp_community", "mgmt_password"):
        if field in result:
            result[field] = protect_secret(result[field])
    return result


async def encrypt_existing_device_secrets() -> None:
    """一次性加密数据库中已存在的明文设备凭据（启用加密密钥后的存量迁移）。

    无有效加密密钥时静默跳过（protect_secret 降级为明文，不破坏数据）。
    """
    from sqlalchemy import select
    from app.database import async_session
    from app.models.device import Device

    async with async_session() as db:
        devices = (await db.execute(select(Device))).scalars().all()
        changed = False
        for d in devices:
            if d.snmp_community and not str(d.snmp_community).startswith(PREFIX):
                d.snmp_community = protect_secret(d.snmp_community)
                changed = True
            if d.mgmt_password and not str(d.mgmt_password).startswith(PREFIX):
                d.mgmt_password = protect_secret(d.mgmt_password)
                changed = True
        if changed:
            await db.commit()
