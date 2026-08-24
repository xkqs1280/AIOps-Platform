"""指标时序 API - 提供设备真实指标的历史数据，供趋势图与健康评分使用。"""
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.metric_record import MetricRecord
from app.models.device import Device

router = APIRouter(prefix="/metrics", tags=["指标时序"])


@router.get("/history")
async def get_metric_history(
    device_id: int = Query(..., description="设备ID"),
    metric_type: str = Query("memory", description="cpu / memory / temperature"),
    hours: int = Query(24, ge=1, le=720, description="回溯小时数"),
    db: AsyncSession = Depends(get_db),
):
    """返回指定设备某指标的历史时序数据（按时间升序）。"""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = await db.execute(
        select(MetricRecord.recorded_at, MetricRecord.value)
        .where(
            MetricRecord.device_id == device_id,
            MetricRecord.metric_type == metric_type,
            MetricRecord.recorded_at >= since,
        )
        .order_by(MetricRecord.recorded_at.asc())
    )
    rows = result.all()
    return {
        "device_id": device_id,
        "metric_type": metric_type,
        "hours": hours,
        "count": len(rows),
        "data": [
            {
                "recorded_at": r[0].isoformat() if r[0] else None,
                "value": r[1],
            }
            for r in rows
        ],
    }


@router.get("/latest")
async def get_latest_metrics(
    device_id: int = Query(..., description="设备ID"),
    db: AsyncSession = Depends(get_db),
):
    """返回指定设备最新的 CPU/内存/温度真实值。"""
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is None:
        return {"device_id": device_id, "memory": None, "cpu": None, "temperature": None}
    return {
        "device_id": device_id,
        "memory": device.memory_usage,
        "cpu": device.cpu_usage,
        "temperature": device.temperature,
        "updated_at": device.last_seen.isoformat() if device.last_seen else None,
    }
