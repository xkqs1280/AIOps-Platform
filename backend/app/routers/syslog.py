"""Syslog 接收与安全事件查询 API 路由"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.p3_security import SecurityEvent
from app.services.syslog_service import ingest_syslog, generate_sample_logs
from app.services.security_service import get_attack_stats
from app.services.rate_limit import limit_ingest

router = APIRouter(prefix="/syslog", tags=["syslog"])


class SyslogIngestRequest(BaseModel):
    raw_log: str = Field(..., min_length=1, max_length=65_536)
    source_ip: str | None = Field(None, max_length=45)


class SyslogGenerateRequest(BaseModel):
    device_id: int | None = None
    count: int = Field(50, ge=1, le=500)


@router.post("")
async def ingest_syslog_endpoint(
    body: SyslogIngestRequest,
    _: None = Depends(limit_ingest),
    session: AsyncSession = Depends(get_session),
):
    """接收一条原始 syslog 消息，解析并归一化后存入安全事件表。"""
    if not body.raw_log.strip():
        raise HTTPException(status_code=400, detail="raw_log 不能为空")
    event = await ingest_syslog(session, body.raw_log, body.source_ip)
    return {
        "id": event.id,
        "device_id": event.device_id,
        "timestamp": event.timestamp.isoformat() if event.timestamp else None,
        "event_category": event.event_category,
        "severity": event.severity,
        "action": event.action,
        "src_ip": event.src_ip,
        "dst_ip": event.dst_ip,
        "description": event.description,
    }


@router.post("/generate")
async def generate_sample_logs_endpoint(
    body: SyslogGenerateRequest,
    session: AsyncSession = Depends(get_session),
):
    """批量生成示例 syslog 数据用于测试和演示。"""
    count = await generate_sample_logs(session, device_id=body.device_id, count=body.count)
    return {"generated": count}


@router.get("/events/stats")
async def get_event_stats(
    session: AsyncSession = Depends(get_session),
):
    """获取安全事件统计信息（默认过去 24 小时）。"""
    return await get_attack_stats(session)


@router.get("/events")
async def list_security_events(
    device_id: int | None = Query(None, description="设备 ID 过滤"),
    event_category: str | None = Query(None, description="事件类别过滤"),
    severity: str | None = Query(None, description="严重级别过滤"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    """查询安全事件列表，支持按设备、类别、严重级别过滤及分页。"""
    query = select(SecurityEvent)
    if device_id is not None:
        query = query.where(SecurityEvent.device_id == device_id)
    if event_category is not None:
        query = query.where(SecurityEvent.event_category == event_category)
    if severity is not None:
        query = query.where(SecurityEvent.severity == severity)
    query = query.order_by(SecurityEvent.timestamp.desc()).offset(offset).limit(limit)

    result = await session.execute(query)
    events = result.scalars().all()

    return [
        {
            "id": e.id,
            "device_id": e.device_id,
            "timestamp": e.timestamp.isoformat() if e.timestamp else None,
            "event_category": e.event_category,
            "event_subcategory": e.event_subcategory,
            "severity": e.severity,
            "action": e.action,
            "description": e.description,
            "src_ip": e.src_ip,
            "src_port": e.src_port,
            "dst_ip": e.dst_ip,
            "dst_port": e.dst_port,
            "protocol": e.protocol,
            "threat_type": e.threat_type,
        }
        for e in events
    ]
