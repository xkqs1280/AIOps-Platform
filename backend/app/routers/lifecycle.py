"""生命周期管理 API 路由 — EOS/EOL 数据管理、提醒、种子数据"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.lifecycle_service import (
    get_lifecycle_db,
    add_lifecycle_entry,
    update_lifecycle_entry,
    delete_lifecycle_entry,
    get_lifecycle_reminders,
    seed_lifecycle_data,
)

router = APIRouter(prefix="/lifecycle", tags=["生命周期管理"])


class LifecycleBatchCreate(BaseModel):
    """从已纳管设备批量设置 EOS/EOL 的请求体"""
    device_ids: list[int]
    eos_date: str | None = None
    eol_date: str | None = None
    source: str = "manual"


class LifecycleEntryCreate(BaseModel):
    """新增厂商型号 EOS/EOL 记录请求体"""
    vendor: str
    model: str
    eos_date: str | None = None
    eol_date: str | None = None
    source: str = "manual"


# ── 生命周期数据库 CRUD ────────────────────────────────────────────

@router.get("/db")
async def list_lifecycle_entries(
    vendor: str | None = Query(None, description="厂商过滤"),
    search: str | None = Query(None, description="型号模糊搜索"),
    session: AsyncSession = Depends(get_db),
):
    """列出生命周期数据库条目，可按厂商过滤或按型号模糊搜索。"""
    entries = await get_lifecycle_db(session, vendor=vendor, search=search)
    return [
        {
            "id": e.id,
            "vendor": e.vendor,
            "model": e.model,
            "eos_date": e.eos_date.isoformat() if e.eos_date else None,
            "eol_date": e.eol_date.isoformat() if e.eol_date else None,
            "eos_announce": e.eos_announce,
            "source": e.source,
            "updated_at": e.updated_at.isoformat() if e.updated_at else None,
        }
        for e in entries
    ]


@router.post("/db", status_code=201)
async def create_lifecycle_entry(
    payload: LifecycleEntryCreate,
    session: AsyncSession = Depends(get_db),
):
    """新增生命周期条目。"""
    from datetime import date as date_type

    entry = await add_lifecycle_entry(
        session,
        vendor=payload.vendor,
        model=payload.model,
        eos_date=date_type.fromisoformat(payload.eos_date) if payload.eos_date else None,
        eol_date=date_type.fromisoformat(payload.eol_date) if payload.eol_date else None,
        source=payload.source,
    )
    await session.commit()
    return {
        "id": entry.id,
        "vendor": entry.vendor,
        "model": entry.model,
        "eos_date": entry.eos_date.isoformat() if entry.eos_date else None,
        "eol_date": entry.eol_date.isoformat() if entry.eol_date else None,
        "eos_announce": entry.eos_announce,
        "source": entry.source,
    }


@router.post("/db/batch", status_code=201)
async def create_lifecycle_batch(
    payload: LifecycleBatchCreate,
    session: AsyncSession = Depends(get_db),
):
    """从已纳管设备批量创建生命周期记录：
    - 按 (vendor, model) 去重，upsert 厂商型号库（lifecycle_db，已存在则更新日期）；
    - 同时回写所选设备的 eos_date / eol_date 字段（大屏/详情页生命周期提醒生效）。
    """
    from datetime import date as date_type
    from sqlalchemy import select
    from app.models.device import Device
    from app.models.p2_baseline import LifecycleDB

    device_ids = payload.device_ids
    if not device_ids:
        raise HTTPException(status_code=422, detail="请至少选择一台设备")

    d_eos = date_type.fromisoformat(payload.eos_date) if payload.eos_date else None
    d_eol = date_type.fromisoformat(payload.eol_date) if payload.eol_date else None

    result = await session.execute(select(Device).where(Device.id.in_(device_ids)))
    devices = result.scalars().all()
    if not devices:
        raise HTTPException(status_code=404, detail="所选设备不存在")

    # 1. 回写设备 EOS/EOL
    for d in devices:
        if d_eos:
            d.eos_date = d_eos
        if d_eol:
            d.eol_date = d_eol

    # 2. 按型号去重，upsert 厂商型号库
    model_map = {}
    for d in devices:
        if d.vendor and d.model:
            key = (d.vendor, d.model)
            model_map[key] = model_map.get(key, 0) + 1

    created = updated = 0
    for (vendor, model), cnt in model_map.items():
        existing = (
            await session.execute(
                select(LifecycleDB).where(
                    LifecycleDB.vendor == vendor, LifecycleDB.model == model
                )
            )
        ).scalar_one_or_none()
        if existing:
            if d_eos:
                existing.eos_date = d_eos
            if d_eol:
                existing.eol_date = d_eol
            updated += 1
        else:
            await add_lifecycle_entry(session, vendor, model, d_eos, d_eol, payload.source)
            created += 1

    await session.commit()
    return {
        "device_count": len(devices),
        "model_count": len(model_map),
        "created": created,
        "updated": updated,
        "eos_date": d_eos.isoformat() if d_eos else None,
        "eol_date": d_eol.isoformat() if d_eol else None,
    }


@router.put("/db/{entry_id}")
async def update_lifecycle_entry_endpoint(
    entry_id: int,
    eos_date: str | None = None,
    eol_date: str | None = None,
    eos_announce: str | None = None,
    source: str | None = None,
    session: AsyncSession = Depends(get_db),
):
    """更新生命周期条目。"""
    from datetime import date as date_type

    kwargs = {}
    if eos_date is not None:
        kwargs["eos_date"] = date_type.fromisoformat(eos_date) if eos_date else None
    if eol_date is not None:
        kwargs["eol_date"] = date_type.fromisoformat(eol_date) if eol_date else None
    if eos_announce is not None:
        kwargs["eos_announce"] = eos_announce
    if source is not None:
        kwargs["source"] = source

    entry = await update_lifecycle_entry(session, entry_id, **kwargs)
    if entry is None:
        raise HTTPException(status_code=404, detail="生命周期条目不存在")
    await session.commit()
    return {
        "id": entry.id,
        "vendor": entry.vendor,
        "model": entry.model,
        "eos_date": entry.eos_date.isoformat() if entry.eos_date else None,
        "eol_date": entry.eol_date.isoformat() if entry.eol_date else None,
        "eos_announce": entry.eos_announce,
        "source": entry.source,
    }


@router.delete("/db/{entry_id}", status_code=204)
async def delete_lifecycle_entry_endpoint(
    entry_id: int,
    session: AsyncSession = Depends(get_db),
):
    """删除生命周期条目。"""
    deleted = await delete_lifecycle_entry(session, entry_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="生命周期条目不存在")
    await session.commit()


# ── 生命周期提醒 ───────────────────────────────────────────────────

@router.get("/reminders")
async def list_lifecycle_reminders(
    session: AsyncSession = Depends(get_db),
):
    """获取所有设备的生命周期提醒（保修到期 / EOS 临近 / EOL 临近 / 已过 EOL）。"""
    reminders = await get_lifecycle_reminders(session)
    return reminders


# ── 种子数据 ────────────────────────────────────────────────────────

@router.post("/seed")
async def seed_lifecycle(
    session: AsyncSession = Depends(get_db),
):
    """插入初始生命周期数据（H3C / 华为 / 深信服常见型号）。已存在则跳过。"""
    await seed_lifecycle_data(session)
    await session.commit()
    return {"message": "种子数据已写入"}
