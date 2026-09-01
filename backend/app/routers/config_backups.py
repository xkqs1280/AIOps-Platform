from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.device import Device
from app.models.config_backup import ConfigBackup, BackupSchedule
from app.routers.auth import operator_or_admin
from app.schemas.config_backup import (
    ConfigBackupResponse, ConfigBackupDetail, ConfigBackupListResponse,
    DiffResponse,
    BackupScheduleCreate, BackupScheduleUpdate,
    BackupScheduleResponse, BackupScheduleListResponse,
)
from app.services.backup_service import perform_backup, generate_diff, calculate_next_backup

router = APIRouter(prefix="/config-backups", tags=["配置备份"])


# === 备份记录 ===

@router.get("", response_model=ConfigBackupListResponse)
async def list_backups(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    device_id: int | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """获取配置备份记录列表"""
    query = select(ConfigBackup)
    count_query = select(func.count(ConfigBackup.id))

    if device_id:
        query = query.where(ConfigBackup.device_id == device_id)
        count_query = count_query.where(ConfigBackup.device_id == device_id)
    if status:
        query = query.where(ConfigBackup.status == status)
        count_query = count_query.where(ConfigBackup.status == status)

    total = (await db.execute(count_query)).scalar()
    offset = (page - 1) * page_size
    result = await db.execute(query.order_by(desc(ConfigBackup.created_at)).offset(offset).limit(page_size))
    items = result.scalars().all()

    return ConfigBackupListResponse(total=total, items=[ConfigBackupResponse.model_validate(b) for b in items])


@router.get("/{backup_id}", response_model=ConfigBackupDetail)
async def get_backup(backup_id: int, user: dict = Depends(operator_or_admin), db: AsyncSession = Depends(get_db)):
    """获取单条备份记录（含配置内容）。

    配置内容含 enable 密码 / VPN 密钥 / ACL 等高敏信息，
    仅 operator 及以上角色可读（viewer 只读角色无权）。
    """
    result = await db.execute(select(ConfigBackup).where(ConfigBackup.id == backup_id))
    backup = result.scalar_one_or_none()
    if not backup:
        raise HTTPException(status_code=404, detail="备份记录不存在")
    return ConfigBackupDetail.model_validate(backup)


@router.post("/manual/{device_id}", response_model=ConfigBackupResponse)
async def trigger_manual_backup(device_id: int, user: dict = Depends(operator_or_admin), db: AsyncSession = Depends(get_db)):
    """手动触发设备配置备份（operator 及以上）。"""
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")

    backup = await perform_backup(db, device_id, backup_type="manual")

    if backup.status == "failed":
        raise HTTPException(status_code=422, detail=f"备份失败: {backup.error_message}")

    return ConfigBackupResponse.model_validate(backup)


@router.post("/manual-all")
async def trigger_backup_all(user: dict = Depends(operator_or_admin), db: AsyncSession = Depends(get_db)):
    """手动触发全部设备配置备份（operator 及以上；全量触发，限制高权限角色）。"""
    result = await db.execute(select(Device))
    all_devices = result.scalars().all()

    results = []
    for dev in all_devices:
        try:
            backup = await perform_backup(db, dev.id, backup_type="manual")
            results.append({
                "device_id": dev.id,
                "device_name": dev.name,
                "ip": dev.ip,
                "status": backup.status,
                "file_size": backup.file_size,
                "line_count": backup.line_count,
                "error": backup.error_message,
            })
        except Exception as e:
            results.append({
                "device_id": dev.id,
                "device_name": dev.name,
                "ip": dev.ip,
                "status": "failed",
                "error": str(e)[:500],
            })

    success_count = sum(1 for r in results if r["status"] == "success")
    return {"total": len(results), "success": success_count, "failed": len(results) - success_count, "results": results}


@router.delete("/{backup_id}")
async def delete_backup(backup_id: int, user: dict = Depends(operator_or_admin), db: AsyncSession = Depends(get_db)):
    """删除备份记录（HTTP 中间件已要求 admin，此处显式防御纵深）。"""
    result = await db.execute(select(ConfigBackup).where(ConfigBackup.id == backup_id))
    backup = result.scalar_one_or_none()
    if not backup:
        raise HTTPException(status_code=404, detail="备份记录不存在")

    await db.delete(backup)
    await db.commit()
    return {"detail": "已删除"}


@router.get("/compare/{backup_id1}/{backup_id2}", response_model=DiffResponse)
async def compare_backups(backup_id1: int, backup_id2: int, user: dict = Depends(operator_or_admin), db: AsyncSession = Depends(get_db)):
    """对比两个备份版本的配置差异（配置内容高敏，仅 operator 及以上）。"""
    result1 = await db.execute(select(ConfigBackup).where(ConfigBackup.id == backup_id1))
    backup1 = result1.scalar_one_or_none()
    if not backup1:
        raise HTTPException(status_code=404, detail=f"备份 {backup_id1} 不存在")

    result2 = await db.execute(select(ConfigBackup).where(ConfigBackup.id == backup_id2))
    backup2 = result2.scalar_one_or_none()
    if not backup2:
        raise HTTPException(status_code=404, detail=f"备份 {backup_id2} 不存在")

    diff = generate_diff(backup1.config_content, backup2.config_content)
    return DiffResponse(backup1_id=backup_id1, backup2_id=backup_id2, diff=diff)


# === 备份计划 ===

@router.get("/schedules/list", response_model=BackupScheduleListResponse)
async def list_schedules(
    device_id: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    """获取备份计划列表"""
    query = select(BackupSchedule).options(selectinload(BackupSchedule.device))
    if device_id:
        query = query.where(BackupSchedule.device_id == device_id)

    result = await db.execute(query.order_by(desc(BackupSchedule.created_at)))
    items = result.scalars().all()

    resp_items = []
    for s in items:
        resp = BackupScheduleResponse.model_validate(s)
        resp.device_name = "全部设备" if s.is_all_devices else (s.device.name if s.device else f"Device #{s.device_id}")
        resp_items.append(resp)

    return BackupScheduleListResponse(total=len(resp_items), items=resp_items)


@router.post("/schedules", response_model=BackupScheduleResponse)
async def create_schedule(data: BackupScheduleCreate, user: dict = Depends(operator_or_admin), db: AsyncSession = Depends(get_db)):
    """创建备份计划（operator 及以上）。"""
    # Validate: either is_all_devices or device_id must be set
    if not data.is_all_devices and not data.device_id:
        raise HTTPException(status_code=422, detail="请选择设备或全部设备")

    if not data.is_all_devices:
        # Check device exists
        result = await db.execute(select(Device).where(Device.id == data.device_id))
        device = result.scalar_one_or_none()
        if not device:
            raise HTTPException(status_code=404, detail="设备不存在")

        # Check if schedule already exists for this device
        existing = await db.execute(
            select(BackupSchedule).where(
                BackupSchedule.device_id == data.device_id,
                BackupSchedule.is_all_devices == False,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="该设备已有备份计划，请编辑现有计划")
    else:
        # Check if all-devices schedule already exists
        existing = await db.execute(
            select(BackupSchedule).where(BackupSchedule.is_all_devices == True)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="全部设备的备份计划已存在，请编辑现有计划")

    # Validate frequency-specific fields
    if data.frequency == "weekly" and data.day_of_week is None:
        data.day_of_week = 0
    if data.frequency == "monthly" and data.day_of_month is None:
        data.day_of_month = 1

    schedule = BackupSchedule(**data.model_dump())
    schedule.next_backup_at = calculate_next_backup(schedule)

    db.add(schedule)
    await db.commit()
    await db.refresh(schedule)

    resp = BackupScheduleResponse.model_validate(schedule)
    resp.device_name = "全部设备" if schedule.is_all_devices else (schedule.device.name if schedule.device else f"Device #{schedule.device_id}")
    return resp


@router.put("/schedules/{schedule_id}", response_model=BackupScheduleResponse)
async def update_schedule(schedule_id: int, data: BackupScheduleUpdate, user: dict = Depends(operator_or_admin), db: AsyncSession = Depends(get_db)):
    """更新备份计划（operator 及以上）。"""
    result = await db.execute(
        select(BackupSchedule).options(selectinload(BackupSchedule.device)).where(BackupSchedule.id == schedule_id)
    )
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="备份计划不存在")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(schedule, key, value)

    # Recalculate next backup time
    schedule.next_backup_at = calculate_next_backup(schedule)

    await db.commit()
    await db.refresh(schedule)

    resp = BackupScheduleResponse.model_validate(schedule)
    resp.device_name = "全部设备" if schedule.is_all_devices else (schedule.device.name if schedule.device else f"Device #{schedule.device_id}")
    return resp


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: int, user: dict = Depends(operator_or_admin), db: AsyncSession = Depends(get_db)):
    """删除备份计划（HTTP 中间件已要求 admin，此处显式防御纵深）。"""
    result = await db.execute(select(BackupSchedule).where(BackupSchedule.id == schedule_id))
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="备份计划不存在")

    await db.delete(schedule)
    await db.commit()
    return {"detail": "已删除"}
