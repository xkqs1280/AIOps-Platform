from fastapi import APIRouter, Depends, Query, HTTPException, status, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
import asyncio
import json

from app.database import get_db, async_session
from app.models.alert import Alert
from app.schemas.alert import AlertResponse, AlertListResponse, AlertStats

router = APIRouter(prefix="/alerts", tags=["告警管理"])


def _alert_item(a: Alert) -> dict:
    return {
        "id": a.id,
        "device_id": a.device_id,
        "device_name": a.device.name if a.device else None,
        "ip": a.device.ip if a.device else None,
        "rule_name": a.rule_name,
        "severity": a.severity,
        "message": a.message,
        "status": a.status,
        "triggered_at": a.triggered_at.isoformat() if a.triggered_at else None,
        "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
    }


@router.get("", response_model=AlertListResponse)
async def list_alerts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    severity: str | None = None,
    status: str | None = None,
    device_id: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(Alert).options(joinedload(Alert.device))
    count_query = select(func.count(Alert.id))

    if severity:
        query = query.where(Alert.severity == severity)
        count_query = count_query.where(Alert.severity == severity)
    if status:
        query = query.where(Alert.status == status)
        count_query = count_query.where(Alert.status == status)
    if device_id:
        query = query.where(Alert.device_id == device_id)
        count_query = count_query.where(Alert.device_id == device_id)

    total = (await db.execute(count_query)).scalar()
    offset = (page - 1) * page_size
    result = await db.execute(query.order_by(Alert.triggered_at.desc()).offset(offset).limit(page_size))
    items = result.unique().scalars().all()

    return AlertListResponse(
        total=total,
        items=[
            AlertResponse(
                id=a.id,
                device_id=a.device_id,
                device_name=a.device.name if a.device else None,
                rule_name=a.rule_name,
                severity=a.severity,
                message=a.message,
                status=a.status,
                triggered_at=a.triggered_at,
                resolved_at=a.resolved_at,
            )
            for a in items
        ],
    )


@router.get("/stats", response_model=AlertStats)
async def alert_stats(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(
            Alert.severity,
            func.count(Alert.id).label("cnt"),
        )
        .where(Alert.status == "active")
        .group_by(Alert.severity)
    )
    stats = AlertStats()
    for row in result.all():
        sev = row.severity
        cnt = row.cnt
        stats.total_active += cnt
        if sev == "critical":
            stats.critical = cnt
        elif sev == "major":
            stats.major = cnt
        elif sev == "minor":
            stats.minor = cnt
        elif sev == "warning":
            stats.warning = cnt
    return stats


@router.get("/stream")
async def alert_stream(request: Request):
    """SSE 实时告警推送（新告警 / 恢复事件，秒级）。

    服务端聚合轮询（2s）替代前端轮询；EventSource 同源自动携带登录 cookie，
    未登录由全局认证中间件拦截返回 401。首次连接推送 init 事件（当前游标），
    前端只记录不播报历史告警。

    注意：流内不使用 Depends(get_db) 的会话（会长期占用连接池），
    改为每次轮询临时开短连接，流生命周期不占池连接。
    """
    async def event_gen():
        async with async_session() as db:
            max_id = (await db.execute(select(func.max(Alert.id)))).scalar() or 0
            max_res = (await db.execute(select(func.max(Alert.resolved_at)))).scalar()
        yield f"data: {json.dumps({'type': 'init', 'last_id': max_id}, ensure_ascii=False)}\n\n"
        last_alert_id = max_id
        last_resolved_at = max_res
        try:
            while True:
                if await request.is_disconnected():
                    break
                events: list[tuple[str, dict]] = []
                async with async_session() as db:
                    # 新告警（id 增量）
                    result = await db.execute(
                        select(Alert)
                        .options(joinedload(Alert.device))
                        .where(Alert.id > last_alert_id)
                        .order_by(Alert.id.asc())
                        .limit(100)
                    )
                    items = result.unique().scalars().all()
                    for a in items:
                        events.append(("alert", _alert_item(a)))
                    if items:
                        last_alert_id = max(a.id for a in items)
                    # 恢复事件（resolved_at 增量）
                    q = (
                        select(Alert)
                        .options(joinedload(Alert.device))
                        .where(Alert.status == "resolved", Alert.resolved_at.isnot(None))
                    )
                    if last_resolved_at:
                        q = q.where(Alert.resolved_at > last_resolved_at)
                    result = await db.execute(q.order_by(Alert.resolved_at.asc()).limit(100))
                    recovered = result.unique().scalars().all()
                    for a in recovered:
                        events.append(("recovered", _alert_item(a)))
                    if recovered:
                        last_resolved_at = max(a.resolved_at for a in recovered if a.resolved_at)
                for typ, item in events:
                    yield f"data: {json.dumps({'type': typ, 'item': item}, ensure_ascii=False)}\n\n"
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("", status_code=status.HTTP_200_OK)
async def clear_alerts(db: AsyncSession = Depends(get_db)):
    """一键清空全部告警记录。返回清理数量。"""
    count = (
        await db.execute(select(func.count(Alert.id)))
    ).scalar() or 0
    await db.execute(Alert.__table__.delete())
    await db.commit()
    return {"deleted": count}


@router.delete("/{alert_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_alert(alert_id: int, db: AsyncSession = Depends(get_db)):
    """删除单条告警记录。"""
    alert = (
        await db.execute(select(Alert).where(Alert.id == alert_id))
    ).scalar_one_or_none()
    if not alert:
        raise HTTPException(status_code=404, detail="告警不存在")
    await db.delete(alert)
    await db.commit()
    return None
