# -*- coding: utf-8 -*-
"""告警通知通道 API 路由：钉钉 / 企业微信 / 飞书 / 自定义 Webhook 的增删改查与测试发送。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.notify_channel import CHANNEL_LABELS, CHANNEL_TYPES
from app.routers.auth import admin_only
from app.services.audit_service import record_audit
from app.services.notify_service import (
    delete_channel,
    list_channels,
    save_channel,
    test_channel,
)

router = APIRouter(prefix="/notify-channels", tags=["通知通道"])


class ChannelRequest(BaseModel):
    name: str = Field("", max_length=64)
    channel_type: str = Field(..., max_length=16)
    webhook_url: str = Field("", max_length=1024)  # 掩码回显时视为未修改
    secret: str = Field("", max_length=512)
    enabled: bool = True
    min_severity: str = Field("warning", max_length=16)
    mention_all: bool = False
    retry_times: int = 1


class ChannelUpdate(BaseModel):
    """更新通道：所有字段可选，仅传入的字段被修改（PATCH 语义的 PUT）。

    与项目其他 *Update 模型一致。掩码回显（含 ****）的 webhook_url/secret
    视为「未修改」；显式传空串则清空。
    """
    name: str | None = Field(None, max_length=64)
    channel_type: str | None = Field(None, max_length=16)
    webhook_url: str | None = Field(None, max_length=1024)
    secret: str | None = Field(None, max_length=512)
    enabled: bool | None = None
    min_severity: str | None = Field(None, max_length=16)
    mention_all: bool | None = None
    retry_times: int | None = None


@router.get("/meta")
async def channel_meta(_: dict = Depends(admin_only)):
    """可选通道类型与级别（供前端下拉框使用）。"""
    return {
        "types": [{"value": t, "label": CHANNEL_LABELS.get(t, t)} for t in CHANNEL_TYPES],
        "severities": [{"value": s, "label": v} for s, v in (
            ("info", "提示"), ("warning", "警告"), ("minor", "次要"),
            ("major", "重要"), ("critical", "严重"),
        )],
        "note": "仅推送严重级别 >= 所选级别的告警",
    }


@router.get("")
async def read_channels(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(admin_only),
):
    """列出所有通知通道（webhook 与 secret 返回掩码）。"""
    return await list_channels(db)


@router.post("", status_code=201)
async def create_channel(
    body: ChannelRequest,
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(admin_only),
):
    try:
        result = await save_channel(db, body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await record_audit(db, actor, "notify", "create", f"新增通知通道 {result.get('name')}")
    return result


@router.put("/{channel_id}")
async def update_channel(
    channel_id: int,
    body: ChannelUpdate,
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(admin_only),
):
    """局部更新：仅修改请求中出现的字段（如只改 enabled 不必回传全部字段）。"""
    try:
        result = await save_channel(
            db, body.model_dump(exclude_unset=True), channel_id=channel_id
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if result is None:
        raise HTTPException(status_code=404, detail="通道不存在")
    await record_audit(db, actor, "notify", "update", f"更新通知通道 {result.get('name')}")
    return result


@router.delete("/{channel_id}", status_code=204)
async def remove_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(admin_only),
):
    ok = await delete_channel(db, channel_id)
    if not ok:
        raise HTTPException(status_code=404, detail="通道不存在")
    await record_audit(db, actor, "notify", "delete", f"删除通知通道 #{channel_id}")
    return None


@router.post("/{channel_id}/test")
async def test_notify_channel(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(admin_only),
):
    """向指定通道发送一条测试消息，验证配置是否正确。"""
    result = await test_channel(db, channel_id)
    await record_audit(
        db, actor, "notify", "test",
        f"测试通知通道 #{channel_id}：{'成功' if result.get('sent') else '失败'}",
    )
    if not result.get("sent"):
        raise HTTPException(status_code=400, detail=result.get("reason") or "发送失败")
    return result
