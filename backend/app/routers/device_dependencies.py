# -*- coding: utf-8 -*-
"""设备依赖关系 API 路由 —— 支撑告警拓扑依赖抑制。

运维语义：声明「A 依赖 B」（A 是下游/接入侧，B 是上游/汇聚侧）。
当 B 不可达时，A 因失去上联产生的连带告警会被标记为 suppressed，避免刷屏。
未配置任何依赖时抑制逻辑不生效。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.device import Device
from app.models.device_dependency import DeviceDependency
from app.routers.auth import admin_only
from app.services.audit_service import record_audit

router = APIRouter(prefix="/device-dependencies", tags=["设备依赖"])


class DependencyRequest(BaseModel):
    device_id: int = Field(..., description="下游设备（产生连带告警的一方）")
    depends_on_device_id: int = Field(..., description="上游设备（不可达时抑制下游告警）")
    enabled: bool = True
    note: str | None = Field(None, max_length=255)


def _to_dict(row: DeviceDependency) -> dict:
    return {
        "id": row.id,
        "device_id": row.device_id,
        "device_name": row.device.name if row.device else str(row.device_id),
        "depends_on_device_id": row.depends_on_device_id,
        "depends_on_name": row.depends_on.name if row.depends_on else str(row.depends_on_device_id),
        "enabled": row.enabled,
        "note": row.note,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def _device_map(db: AsyncSession) -> dict[int, str]:
    rows = (await db.execute(select(Device.id, Device.name))).all()
    return {r[0]: r[1] for r in rows}


async def _would_create_cycle(db: AsyncSession, device_id: int, depends_on_id: int) -> bool:
    """检测新增依赖是否形成环（A→B→A）。

    环会让两端互相抑制，故障时告警全部消失，因此必须拒绝。
    从 depends_on_id 出发沿「依赖的上游」方向走，若能到达 device_id 则成环。
    """
    rows = (await db.execute(
        select(DeviceDependency.device_id, DeviceDependency.depends_on_device_id)
    )).all()
    graph: dict[int, list[int]] = {}
    for down, up in rows:
        graph.setdefault(down, []).append(up)

    stack = [depends_on_id]
    seen: set[int] = set()
    while stack:
        cur = stack.pop()
        if cur == device_id:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(graph.get(cur, []))
    return False


@router.get("")
async def list_dependencies(
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(admin_only),
):
    rows = (await db.execute(
        select(DeviceDependency).order_by(DeviceDependency.id)
    )).scalars().all()
    return [_to_dict(r) for r in rows]


@router.get("/device/{device_id}")
async def list_device_dependencies(
    device_id: int,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(admin_only),
):
    """某台设备的上游依赖列表（设备详情页展示用）。"""
    rows = (await db.execute(
        select(DeviceDependency).where(DeviceDependency.device_id == device_id)
    )).scalars().all()
    return [_to_dict(r) for r in rows]


@router.post("", status_code=201)
async def create_dependency(
    body: DependencyRequest,
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(admin_only),
):
    if body.device_id == body.depends_on_device_id:
        raise HTTPException(status_code=400, detail="设备不能依赖自身")

    names = await _device_map(db)
    if body.device_id not in names:
        raise HTTPException(status_code=404, detail=f"设备 #{body.device_id} 不存在")
    if body.depends_on_device_id not in names:
        raise HTTPException(status_code=404, detail=f"设备 #{body.depends_on_device_id} 不存在")

    dup = (await db.execute(
        select(DeviceDependency).where(
            DeviceDependency.device_id == body.device_id,
            DeviceDependency.depends_on_device_id == body.depends_on_device_id,
        )
    )).scalars().first()
    if dup is not None:
        raise HTTPException(status_code=409, detail="该依赖关系已存在")

    if await _would_create_cycle(db, body.device_id, body.depends_on_device_id):
        raise HTTPException(
            status_code=400,
            detail="该依赖会形成环路（互相依赖会导致故障时告警被全部抑制），已拒绝",
        )

    row = DeviceDependency(
        device_id=body.device_id,
        depends_on_device_id=body.depends_on_device_id,
        enabled=body.enabled,
        note=body.note,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await record_audit(
        db, actor, "device", "dependency_add",
        f"{names.get(body.device_id)} 依赖 {names.get(body.depends_on_device_id)}",
    )
    return _to_dict(row)


@router.put("/{dep_id}")
async def update_dependency(
    dep_id: int,
    body: DependencyRequest,
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(admin_only),
):
    row = (await db.execute(
        select(DeviceDependency).where(DeviceDependency.id == dep_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="依赖关系不存在")

    if body.device_id != row.device_id or body.depends_on_device_id != row.depends_on_device_id:
        if body.device_id == body.depends_on_device_id:
            raise HTTPException(status_code=400, detail="设备不能依赖自身")
        if await _would_create_cycle(db, body.device_id, body.depends_on_device_id):
            raise HTTPException(status_code=400, detail="该依赖会形成环路，已拒绝")
        row.device_id = body.device_id
        row.depends_on_device_id = body.depends_on_device_id

    row.enabled = body.enabled
    row.note = body.note
    await db.commit()
    await db.refresh(row)
    await record_audit(db, actor, "device", "dependency_update", f"更新依赖关系 #{dep_id}")
    return _to_dict(row)


@router.delete("/{dep_id}", status_code=204)
async def delete_dependency(
    dep_id: int,
    db: AsyncSession = Depends(get_db),
    actor: dict = Depends(admin_only),
):
    row = (await db.execute(
        select(DeviceDependency).where(DeviceDependency.id == dep_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="依赖关系不存在")
    await db.delete(row)
    await db.commit()
    await record_audit(db, actor, "device", "dependency_delete", f"删除依赖关系 #{dep_id}")
    return None
