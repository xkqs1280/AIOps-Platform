# -*- coding: utf-8 -*-
"""访问控制 API：平台访问 IP 白名单（管理员维护）。

真正的**整站拦截在** ``main.py`` 的 HTTP 中间件里做（所有请求都过，含前端静态资源、
WebSocket 另在终端端点自查）。这个路由只负责「读配置 / 改配置」。
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.access_control import AccessControlSetting, IpWhitelistEntry
from app.routers.auth import admin_only
from app.services import ip_whitelist
from app.services.audit_service import record_audit

router = APIRouter(prefix="/access-control", tags=["访问控制"])

MAX_ENTRIES = 200


class WhitelistEntryIn(BaseModel):
    cidr: str = Field("", max_length=64)
    remark: str = Field("", max_length=64)


class WhitelistRequest(BaseModel):
    enabled: bool = False
    entries: list[WhitelistEntryIn] = Field(default_factory=list)


async def _load(db: AsyncSession):
    row = (
        await db.execute(select(AccessControlSetting).order_by(AccessControlSetting.id).limit(1))
    ).scalars().first()
    rows = (
        await db.execute(select(IpWhitelistEntry).order_by(IpWhitelistEntry.id))
    ).scalars().all()
    return row, rows


def _serialize(entries) -> list[dict]:
    return [
        {
            "id": entry.id,
            "cidr": entry.cidr,
            "remark": entry.remark or "",
            "created_by": entry.created_by or "",
            "created_at": entry.created_at.isoformat() if entry.created_at else "",
        }
        for entry in entries
    ]


@router.get("/ip-whitelist")
async def read_ip_whitelist(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(admin_only),
):
    """读取白名单配置与本次请求的来源 IP（管理员）。"""
    row, rows = await _load(db)
    return {
        "enabled": bool(row.ip_whitelist_enabled) if row else False,
        "entries": _serialize(rows),
        # 界面要把它显示成「平台看到的您当前来源 IP」并提供一键加入 ——
        # 让管理员不必去猜自己的出口 IP 是多少（这是现场最容易配错的一步）
        "current_ip": ip_whitelist.resolve_client_ip(request),
        "updated_by": (row.updated_by or "") if row else "",
    }


@router.put("/ip-whitelist")
async def save_ip_whitelist(
    body: WhitelistRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(admin_only),
):
    """全量保存白名单（管理员）。

    为什么是「全量保存」而不是单条增删：前端本地维护列表、一次提交即一份原子配置，
    同时让**防自锁校验只有一个入口** —— 只要提交结果里不含自己当前 IP，就不许保存。
    """
    if len(body.entries) > MAX_ENTRIES:
        raise HTTPException(status_code=400, detail=f"白名单条目过多（最多 {MAX_ENTRIES} 条）")
    try:
        normalized = ip_whitelist.normalize_entries([entry.model_dump() for entry in body.entries])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 开启白名单却没有条目 = 把所有人（包括自己）挡在门外，直接拒绝
    if body.enabled and not normalized:
        raise HTTPException(status_code=400, detail="启用白名单前请至少添加一条 IP 或网段")

    current_ip = ip_whitelist.resolve_client_ip(request)
    networks = ip_whitelist.compile_networks([cidr for cidr, _ in normalized])
    # 防自锁：提交后自己必须还能访问。本机回环访问不受名单约束，无需强求。
    if body.enabled and not ip_whitelist.is_loopback_ip(current_ip) and not ip_whitelist.ip_in_networks(current_ip, networks):
        raise HTTPException(
            status_code=400,
            detail=(
                f"当前访问 IP（{current_ip}）不在本次提交的名单内，保存后你会立刻无法访问平台。"
                "请先把该 IP 加入白名单再保存。"
            ),
        )

    row, _rows = await _load(db)
    if row is None:
        db.add(AccessControlSetting(ip_whitelist_enabled=body.enabled, updated_by=actor["sub"]))
    else:
        row.ip_whitelist_enabled = body.enabled
        row.updated_by = actor["sub"]
    # 整表替换：请求体就是权威列表，不做增量 diff（避免出现「删不掉的幽灵条目」）
    await db.execute(delete(IpWhitelistEntry))
    for cidr, remark in normalized:
        db.add(IpWhitelistEntry(cidr=cidr, remark=remark, created_by=actor["sub"]))
    await db.commit()

    ip_whitelist.invalidate()  # 立即生效，不等 TTL

    summary = "、".join(cidr for cidr, _ in normalized[:10]) or "无"
    if len(normalized) > 10:
        summary += f" 等 {len(normalized)} 条"
    await record_audit(
        db,
        actor,
        "access_control",
        "update_ip_whitelist",
        f"{'启用' if body.enabled else '关闭'}访问 IP 白名单；共 {len(normalized)} 条：{summary}；"
        f"操作来源 IP {current_ip}",
        ip=current_ip,
    )
    row_after, rows_after = await _load(db)
    return {
        "enabled": bool(row_after.ip_whitelist_enabled) if row_after else body.enabled,
        "entries": _serialize(rows_after),
        "current_ip": current_ip,
        "updated_by": actor["sub"],
    }
