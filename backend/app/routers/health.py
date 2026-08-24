"""设备健康评分 API 路由 — 评分计算、查询"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.health_service import (
    calculate_health_score,
    calculate_all_health_scores,
    get_all_health_scores,
    get_device_health,
)

router = APIRouter(prefix="/health", tags=["健康评分"])


@router.post("/calculate")
async def trigger_health_calculation(
    device_id: int | None = None,
    session: AsyncSession = Depends(get_db),
):
    """计算健康评分：若传入 device_id 则只计算指定设备，否则全量计算。"""
    if device_id is not None:
        result = await calculate_health_score(session, device_id)
        if result is None:
            raise HTTPException(status_code=404, detail="设备不存在")
        return {"message": "健康评分计算完成", "result": result}
    else:
        count = await calculate_all_health_scores(session)
        return {"message": "全量健康评分计算完成", "device_count": count}


@router.get("")
async def list_health_scores(
    min_score: int | None = Query(None, ge=0, le=100, description="最低分过滤"),
    session: AsyncSession = Depends(get_db),
):
    """列出所有设备健康评分，可按最低分过滤（最差优先排列）。"""
    scores = await get_all_health_scores(session, min_score=min_score)
    return scores


@router.get("/{device_id}")
async def get_device_health_endpoint(
    device_id: int,
    session: AsyncSession = Depends(get_db),
):
    """获取指定设备的健康评分详情。"""
    result = await get_device_health(session, device_id)
    if result is None:
        raise HTTPException(status_code=404, detail="未找到该设备的健康评分")
    return result
