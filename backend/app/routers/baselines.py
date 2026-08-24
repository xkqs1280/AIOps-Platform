"""动态基线 API 路由 — 基线列表、计算触发、设备基线查询"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.p2_baseline import MetricBaseline
from app.services.baseline_service import (
    calculate_baselines,
    detect_baseline_deviation,
    get_device_baselines,
)

router = APIRouter(prefix="/baselines", tags=["动态基线"])


@router.get("")
async def list_baselines(
    device_id: int | None = Query(None, description="设备 ID 过滤"),
    metric_name: str | None = Query(None, description="指标名称过滤"),
    session: AsyncSession = Depends(get_db),
):
    """列出基线记录，支持按设备 ID 和指标名称过滤。"""
    query = select(MetricBaseline)
    if device_id is not None:
        query = query.where(MetricBaseline.device_id == device_id)
    if metric_name is not None:
        query = query.where(MetricBaseline.metric_name == metric_name)
    query = query.order_by(MetricBaseline.device_id, MetricBaseline.metric_name, MetricBaseline.hour_of_day)

    result = await session.execute(query)
    records = result.scalars().all()

    return [
        {
            "id": r.id,
            "device_id": r.device_id,
            "metric_name": r.metric_name,
            "hour_of_day": r.hour_of_day,
            "p5": r.p5,
            "p25": r.p25,
            "p50": r.p50,
            "p75": r.p75,
            "p95": r.p95,
            "stddev": r.stddev,
            "sample_count": r.sample_count,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in records
    ]


@router.post("/calculate")
async def trigger_baseline_calculation(
    device_id: int | None = None,
    session: AsyncSession = Depends(get_db),
):
    """触发基线计算：若传入 device_id 则只计算指定设备，否则全量计算。"""
    count = await calculate_baselines(session, device_id=device_id)
    return {"message": "基线计算完成", "updated_count": count}


@router.get("/{device_id}")
async def get_device_baselines_endpoint(
    device_id: int,
    session: AsyncSession = Depends(get_db),
):
    """获取指定设备的所有基线记录，按 metric_name 分组返回。"""
    result = await get_device_baselines(session, device_id)
    if not result:
        raise HTTPException(status_code=404, detail="未找到该设备的基线记录")
    return {"device_id": device_id, "baselines": result}
