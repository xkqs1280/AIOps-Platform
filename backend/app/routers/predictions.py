"""AI 预测 API 路由 — 预测触发、结果查询"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.p2_baseline import PredictionResult
from app.services.prediction_service import run_all_predictions

router = APIRouter(prefix="/predictions", tags=["AI 预测"])


@router.post("/run")
async def trigger_predictions(
    device_id: int | None = None,
    session: AsyncSession = Depends(get_db),
):
    """触发全量预测：若传入 device_id 则只计算指定设备，否则全量计算。"""
    summary = await run_all_predictions(session, device_id=device_id)
    return summary


@router.get("")
async def list_predictions(
    device_id: int | None = Query(None, description="设备 ID 过滤"),
    prediction_type: str | None = Query(None, description="预测类型过滤"),
    session: AsyncSession = Depends(get_db),
):
    """列出预测结果，支持按设备 ID 和预测类型过滤。"""
    query = select(PredictionResult)
    if device_id is not None:
        query = query.where(PredictionResult.device_id == device_id)
    if prediction_type is not None:
        query = query.where(PredictionResult.prediction_type == prediction_type)
    query = query.order_by(PredictionResult.device_id, PredictionResult.prediction_type)

    result = await session.execute(query)
    records = result.scalars().all()

    return [
        {
            "id": r.id,
            "device_id": r.device_id,
            "prediction_type": r.prediction_type,
            "metric_name": r.metric_name,
            "current_value": r.current_value,
            "predicted_value": r.predicted_value,
            "predicted_date": r.predicted_date.isoformat() if r.predicted_date else None,
            "confidence": r.confidence,
            "details": r.details,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in records
    ]


@router.get("/{device_id}")
async def get_device_predictions(
    device_id: int,
    session: AsyncSession = Depends(get_db),
):
    """获取指定设备的所有预测结果。"""
    result = await session.execute(
        select(PredictionResult)
        .where(PredictionResult.device_id == device_id)
        .order_by(PredictionResult.prediction_type)
    )
    records = result.scalars().all()

    if not records:
        return {"device_id": device_id, "predictions": []}

    return {
        "device_id": device_id,
        "predictions": [
            {
                "id": r.id,
                "prediction_type": r.prediction_type,
                "metric_name": r.metric_name,
                "current_value": r.current_value,
                "predicted_value": r.predicted_value,
                "predicted_date": r.predicted_date.isoformat() if r.predicted_date else None,
                "confidence": r.confidence,
                "details": r.details,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in records
        ],
    }


@router.get("/{device_id}/disk")
async def get_disk_exhaustion_prediction(
    device_id: int,
    session: AsyncSession = Depends(get_db),
):
    """获取指定设备的磁盘耗尽预测结果。"""
    result = await session.execute(
        select(PredictionResult).where(
            PredictionResult.device_id == device_id,
            PredictionResult.prediction_type == "disk_exhaustion",
        )
    )
    record = result.scalar_one_or_none()

    if not record:
        raise HTTPException(status_code=404, detail="未找到该设备的磁盘耗尽预测")

    return {
        "device_id": record.device_id,
        "prediction_type": record.prediction_type,
        "current_value": record.current_value,
        "predicted_value": record.predicted_value,
        "predicted_date": record.predicted_date.isoformat() if record.predicted_date else None,
        "confidence": record.confidence,
        "details": record.details,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }
