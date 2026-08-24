import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.database import get_db
from app.models.device import Device
from app.models.inspection import InspectionTask, InspectionDeviceResult
from app.schemas.inspection import (
    InspectionCreateRequest,
    InspectionTaskResponse,
    InspectionTaskDetailResponse,
    InspectionTaskListResponse,
)
from app.services.h3c_inspection_service import create_inspection_task, _get_report_dir, _run_single_device_inspection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inspections", tags=["H3C 设备巡检"])


@router.get("", response_model=InspectionTaskListResponse)
async def list_inspection_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """列出巡检任务。"""
    count_query = select(func.count(InspectionTask.id))
    total = (await db.execute(count_query)).scalar()

    offset = (page - 1) * page_size
    result = await db.execute(
        select(InspectionTask)
        .order_by(InspectionTask.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    items = result.scalars().all()
    return InspectionTaskListResponse(
        total=total,
        items=[InspectionTaskResponse.model_validate(t) for t in items],
    )


@router.get("/{task_id}", response_model=InspectionTaskDetailResponse)
async def get_inspection_task(task_id: int, db: AsyncSession = Depends(get_db)):
    """获取巡检任务详情，包含每台设备的结果。"""
    result = await db.execute(
        select(InspectionTask)
        .where(InspectionTask.id == task_id)
        .options(joinedload(InspectionTask.device_results))
    )
    task = result.unique().scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="巡检任务不存在")
    return InspectionTaskDetailResponse.model_validate(task)


@router.post("/{task_id}/devices/{device_id}/retry", status_code=202)
async def retry_device_inspection(
    task_id: int,
    device_id: int,
    db: AsyncSession = Depends(get_db),
):
    """重新执行单台设备的巡检（解决设备卡死）。

    - failed / pending 可直接重跑；
    - running 且超过 10 分钟视为卡死，可重跑；
    - running 且不足 10 分钟 → 409 拒绝。
    """
    result = await db.execute(select(InspectionTask).where(InspectionTask.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="巡检任务不存在")

    rr = await db.execute(
        select(InspectionDeviceResult).where(
            InspectionDeviceResult.task_id == task_id,
            InspectionDeviceResult.device_id == device_id,
        )
    )
    dev_result = rr.scalar_one_or_none()
    if not dev_result:
        raise HTTPException(status_code=404, detail="该设备的巡检结果不存在")

    now = datetime.now(timezone.utc)
    created = dev_result.created_at
    if created and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    elapsed = now - created if created else timedelta(0)

    if dev_result.status == "running" and elapsed < timedelta(minutes=10):
        raise HTTPException(
            status_code=409,
            detail="设备执行中（不足10分钟），暂不能重新执行；可稍后再试",
        )

    result_dir = os.path.join(_get_report_dir(), f"task_{task_id}")
    os.makedirs(result_dir, exist_ok=True)
    result_id = dev_result.id

    async def _retry():
        from app.database import async_session

        try:
            async with async_session() as dev_db:
                dev = (
                    await dev_db.execute(
                        select(Device).where(Device.id == device_id)
                    )
                ).scalar_one_or_none()
                if dev is None:
                    raise ValueError("设备不存在")
                # 复用已有结果行，避免重复创建；task 由原请求会话加载，
                # async_session expire_on_commit=False，detached 后仅读已加载字段。
                existing = (
                    await dev_db.execute(
                        select(InspectionDeviceResult).where(
                            InspectionDeviceResult.id == result_id
                        )
                    )
                ).scalar_one_or_none()
                await _run_single_device_inspection(
                    dev_db, task, dev, result_dir, dev_result=existing
                )
        except Exception as e:
            logger.exception(f"Device retry failed: task={task_id} device={device_id}")
            try:
                async with async_session() as udb:
                    r2 = (
                        await udb.execute(
                            select(InspectionDeviceResult).where(
                                InspectionDeviceResult.id == result_id
                            )
                        )
                    ).scalar_one_or_none()
                    if r2:
                        r2.status = "failed"
                        r2.error_message = f"重新执行失败: {type(e).__name__}: {str(e)[:400]}"
                        r2.completed_at = datetime.now(timezone.utc)
                        await udb.commit()
            except Exception:
                logger.exception("Failed to update retry failure")

        # 重跑完成（无论成功/失败）后，立即基于当前全部成功设备重新生成报告，
        # 无需等 30 分钟任务超时。
        await _regenerate_reports(task_id)

    asyncio.create_task(_retry())
    return {
        "status": "restarted",
        "device_id": device_id,
        "device_name": dev_result.device_name,
    }


async def _regenerate_reports(task_id: int):
    """基于该任务当前全部成功设备的结果重新生成巡检报告。

    用于单台设备"重新执行"成功后立即更新报告，避免等待任务级 30 分钟截断。
    """
    from app.database import async_session
    from app.services.h3c_inspection_service import generate_reports

    try:
        async with async_session() as db:
            task = (
                await db.execute(
                    select(InspectionTask).where(InspectionTask.id == task_id)
                )
            ).scalar_one_or_none()
            if not task:
                return

            result = await db.execute(
                select(InspectionDeviceResult).where(
                    InspectionDeviceResult.task_id == task_id,
                    InspectionDeviceResult.status == "success",
                )
            )
            success_results = result.scalars().all()
            all_data = [r.parsed_data for r in success_results if r.parsed_data]

            task.success_count = len(success_results)
            task.failed_count = max(0, task.total_devices - task.success_count)

            if all_data:
                result_dir = os.path.join(_get_report_dir(), f"task_{task_id}")
                os.makedirs(result_dir, exist_ok=True)
                # generate_reports 为 CPU 密集同步函数，放入线程池避免阻塞事件循环
                xlsx_path, docx_path = await asyncio.to_thread(
                    generate_reports,
                    all_data,
                    result_dir,
                    f"h3c_inspection_task{task_id}",
                )
                task.excel_path = xlsx_path
                task.word_path = docx_path
                task.status = "completed"
                task.error_message = None
            else:
                task.error_message = "没有设备巡检成功，无法生成报告"
                task.status = "failed"

            await db.commit()
            logger.info(f"Inspection task {task_id} reports regenerated: {task.success_count}/{task.total_devices}")
    except Exception:
        logger.exception(f"Failed to regenerate reports for task {task_id}")


@router.post("", response_model=InspectionTaskResponse, status_code=201)
async def create_task(data: InspectionCreateRequest, db: AsyncSession = Depends(get_db)):
    """创建巡检任务。

    校验所有设备是否存在且厂商为 H3C，然后后台异步执行。
    """
    devices_result = await db.execute(
        select(Device).where(Device.id.in_(data.device_ids))
    )
    devices = devices_result.scalars().all()
    found_ids = {d.id for d in devices}
    missing = set(data.device_ids) - found_ids
    if missing:
        raise HTTPException(status_code=400, detail=f"设备 ID 不存在: {sorted(missing)}")

    # 允许非 H3C 设备也执行巡检（解析引擎可能也支持华为等 Comware 风格命令），
    # 但建议仅选择 H3C 设备。这里只做存在性校验。
    task = await create_inspection_task(db, data.name, data.device_ids)
    return InspectionTaskResponse.model_validate(task)


@router.delete("/{task_id}", status_code=204)
async def delete_inspection_task(task_id: int, db: AsyncSession = Depends(get_db)):
    """删除巡检记录。

    - 可手动删除已完成/失败/待执行的记录；
    - 执行中（running）超过 1 小时的记录也可删除（视为卡死任务）；
    - 执行中且不足 1 小时的不允许删除，避免后台线程写入已删除记录报错。
    """
    from datetime import datetime, timedelta, timezone

    result = await db.execute(
        select(InspectionTask).where(InspectionTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="巡检记录不存在")

    if task.status == "running":
        start = task.started_at or task.created_at
        if start:
            elapsed = datetime.now(timezone.utc) - start
            if elapsed < timedelta(hours=1):
                raise HTTPException(
                    status_code=409,
                    detail="巡检任务执行中（不足1小时），暂不能删除；可稍后再试",
                )

    # 清理生成的报告文件
    for path_key in ("excel_path", "word_path"):
        path = getattr(task, path_key, None)
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                logger.warning(f"删除巡检报告文件失败: {path}")

    await db.delete(task)  # device_results 级联删除
    await db.commit()
    return None


@router.get("/{task_id}/download/excel")
async def download_excel(task_id: int, db: AsyncSession = Depends(get_db)):
    """下载巡检 Excel 报告。"""
    result = await db.execute(select(InspectionTask).where(InspectionTask.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="巡检任务不存在")
    if not task.excel_path or not os.path.exists(task.excel_path):
        raise HTTPException(status_code=404, detail="Excel 报告尚未生成或文件不存在")

    return FileResponse(
        task.excel_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=os.path.basename(task.excel_path),
    )


@router.get("/{task_id}/download/word")
async def download_word(task_id: int, db: AsyncSession = Depends(get_db)):
    """下载巡检 Word 报告。"""
    result = await db.execute(select(InspectionTask).where(InspectionTask.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="巡检任务不存在")
    if not task.word_path or not os.path.exists(task.word_path):
        raise HTTPException(status_code=404, detail="Word 报告尚未生成或文件不存在")

    return FileResponse(
        task.word_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=os.path.basename(task.word_path),
    )
