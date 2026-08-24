"""等保2.0合规检查 API 路由 — 合规检测、状态查询、评分"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.device import Device
from app.services.compliance_service import (
    run_compliance_check,
    calculate_compliance_score,
    get_compliance_status,
    run_secondary_compliance_check,
    run_secondary_compliance_check_batch,
)

router = APIRouter(prefix="/compliance", tags=["compliance"])


class ComplianceCheckRequest(BaseModel):
    device_id: int | None = None
    device_ids: list[int] | None = Field(None, description="指定设备 ID 列表（等保二级核查），为空表示全部设备")


@router.post("/check")
async def run_check(
    body: ComplianceCheckRequest,
    session: AsyncSession = Depends(get_session),
):
    """执行合规检查。

    - 等保二级配置核查（SSH 采集真实配置）：body.device_ids 给定（含空列表）时，对全部/部分设备批量评估。
    - 兼容旧调用：body.device_id 给定且 device_ids 为空时，走单台（SSH 核查优先，回退指标推断）。
    - 全量（不带任何参数）：对全部设备执行等保二级 SSH 核查。
    """
    # 二级批量核查入口：device_ids 显式给定（None 之外的任何值，包括空数组）
    if body.device_ids is not None or (body.device_id is None and body.device_ids is None):
        if body.device_ids is not None:
            target_ids = body.device_ids or None  # 空数组 = 全部
        else:
            target_ids = None
        result = await run_secondary_compliance_check_batch(session, target_ids)
        return result

    # 兼容：单台设备（device_id 给定且未用批量参数）
    if body.device_id is not None:
        result = await run_secondary_compliance_check(session, body.device_id)
        if result.get("error"):
            raise HTTPException(status_code=404, detail=result["error"])
        return result

    # 兜底：全部设备二级核查
    result = await run_secondary_compliance_check_batch(session, None)
    return result


@router.get("/status")
async def get_status(
    device_id: int | None = Query(None, description="设备 ID，为空时返回全局概要"),
    page: int | None = Query(None, ge=1, description="全局模式分页页码（配合 page_size 使用）"),
    page_size: int | None = Query(None, ge=1, le=200, description="全局模式每页条数（如 20/50/100）"),
    session: AsyncSession = Depends(get_session),
):
    """查询合规状态：单设备明细或全平台概要。

    全局模式支持分页：传 page + page_size 时 devices 只返回当前页，
    total_devices / overall_avg / non_compliant_items 仍为全局统计。
    """
    result = await get_compliance_status(session, device_id=device_id, page=page, page_size=page_size)
    if result is None:
        raise HTTPException(status_code=404, detail=f"设备 {device_id} 不存在")
    return result


@router.get("/score/{device_id}")
async def get_score(
    device_id: int,
    session: AsyncSession = Depends(get_session),
):
    """获取指定设备的合规评分。"""
    score = await calculate_compliance_score(session, device_id)
    if score is None:
        raise HTTPException(status_code=404, detail=f"设备 {device_id} 不存在")
    return score
