# -*- coding: utf-8 -*-
"""操作审计日志服务：提供 record_audit 供各路由在敏感操作处记录。"""
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog


def get_client_ip(request: Request | None) -> str:
    if not request:
        return ""
    # 支持反代 X-Forwarded-For；直接请求取 client host
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    if request.client:
        return request.client.host or ""
    return ""


async def record_audit(
    db: AsyncSession,
    actor: dict | None,
    module: str,
    action: str,
    detail: str = "",
    ip: str = "",
) -> None:
    """记录一条审计日志。失败不抛出，避免影响业务。"""
    try:
        entry = AuditLog(
            user=actor["sub"] if actor and actor.get("sub") else "-",
            role=actor.get("role", "-") if actor else "-",
            module=module,
            action=action,
            detail=detail or "",
            ip=ip or "",
        )
        db.add(entry)
        await db.commit()
    except Exception:
        # 审计失败不应阻断业务，尽力回滚
        try:
            await db.rollback()
        except Exception:
            pass
