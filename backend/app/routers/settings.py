# -*- coding: utf-8 -*-
"""系统设置 API 路由：邮件告警配置、平台外观（大屏标题）、操作审计日志查询。"""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.audit import AuditLog
from app.models.platform_setting import PlatformSetting
from app.routers.auth import admin_only, current_user
from app.services.audit_service import record_audit
from app.services.mail_service import get_mail_setting, save_mail_setting

router = APIRouter(prefix="/settings", tags=["系统设置"])


class MailSettingRequest(BaseModel):
    enabled: bool = False
    smtp_host: str = Field("", max_length=128)
    smtp_port: int = 465
    smtp_user: str = Field("", max_length=128)
    smtp_password: str = Field("", max_length=255)  # 留空表示保持原密码
    use_ssl: bool = True
    sender: str = Field("", max_length=128)
    recipients: str = Field("", max_length=512)


@router.get("/mail")
async def read_mail_setting(db: AsyncSession = Depends(get_db), _: dict = Depends(admin_only)):
    """读取邮件告警配置（管理员）。密码返回掩码。"""
    return await get_mail_setting(db) or {
        "enabled": False, "smtp_host": "", "smtp_port": 465, "smtp_user": "",
        "smtp_password": "", "use_ssl": True, "sender": "", "recipients": "",
    }


@router.post("/mail")
async def update_mail_setting(
    body: MailSettingRequest,
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(admin_only),
):
    """保存邮件告警配置（管理员）。"""
    cfg = await save_mail_setting(db, body.model_dump())
    await record_audit(db, actor, "mail", "update", "保存邮件告警配置")
    return cfg


class PlatformSettingRequest(BaseModel):
    site_name: str = Field("", max_length=64)


async def _get_platform_row(db: AsyncSession) -> PlatformSetting | None:
    """取平台设置单行（没有则返回 None）。"""
    result = await db.execute(select(PlatformSetting).order_by(PlatformSetting.id).limit(1))
    return result.scalars().first()


@router.get("/platform")
async def read_platform_setting(db: AsyncSession = Depends(get_db), _: dict = Depends(current_user)):
    """读取平台外观设置（任意登录用户）。

    监控大屏标题要用这个值渲染，因此读权限放到「登录即可」而不是仅管理员；
    写入才是管理员专属。``site_name`` 为空 = 未自定义，由前端回退到内置默认标题。
    """
    row = await _get_platform_row(db)
    return {
        "site_name": row.site_name if row else "",
        "updated_by": row.updated_by if row else "",
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
    }


@router.put("/platform")
async def update_platform_setting(
    body: PlatformSettingRequest,
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(admin_only),
):
    """保存平台外观设置（管理员）。``site_name`` 传空串表示恢复默认标题。"""
    name = (body.site_name or "").strip()
    row = await _get_platform_row(db)
    if row is None:
        row = PlatformSetting(site_name=name, updated_by=actor["sub"])
        db.add(row)
    else:
        row.site_name = name
        row.updated_by = actor["sub"]
    await db.commit()
    await db.refresh(row)
    await record_audit(
        db, actor, "settings", "update",
        f"修改平台名称（监控大屏标题）：{name or '恢复默认'}",
    )
    return {
        "site_name": row.site_name,
        "updated_by": row.updated_by,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/audit-logs")
async def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    module: str | None = Query(None, description="模块过滤（auth/device/user/license/backup/inspection/compliance/mail）"),
    keyword: str | None = Query(None, description="按用户/操作内容搜索"),
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(admin_only),
):
    """查询操作审计日志（管理员），按时间倒序分页。"""
    conds = []
    if module:
        conds.append(AuditLog.module == module)
    if keyword:
        kw = f"%{keyword}%"
        conds.append(AuditLog.user.ilike(kw) | AuditLog.action.ilike(kw) | AuditLog.detail.ilike(kw))
    base = select(AuditLog)
    count_q = select(func.count(AuditLog.id))
    if conds:
        base = base.where(*conds)
        count_q = count_q.where(*conds)
    total = (await db.execute(count_q)).scalar() or 0
    result = await db.execute(
        base.order_by(AuditLog.id.desc()).offset((page - 1) * page_size).limit(page_size)
    )
    items = result.scalars().all()
    return {
        "total": total,
        "items": [
            {
                "id": a.id,
                "user": a.user,
                "role": a.role,
                "module": a.module,
                "action": a.action,
                "detail": a.detail,
                "ip": a.ip,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in items
        ],
    }
