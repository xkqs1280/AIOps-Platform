"""拓扑发现 API - 真实管理设备节点 + 用户自定义连线

节点来自 devices 表（已加入管理的设备），边来自用户手动建立的自定义连线
（topology_links 表）。Phase 1 的 LLDP 自动发现暂以手动连线补充。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.database import get_db
from app.models.device import Device
from app.models.topology_link import TopologyLink
from app.models.alert import Alert

router = APIRouter(prefix="/topology", tags=["拓扑发现"])


class TopologyLinkCreate(BaseModel):
    source_device_id: int
    target_device_id: int
    link_type: str = Field(default="custom", max_length=32)
    label: str | None = Field(default=None, max_length=64)


@router.get("")
async def get_topology(db: AsyncSession = Depends(get_db)):
    """返回拓扑：节点 = 已管理设备（含状态与实时指标），边 = 自定义连线。

    节点状态优先级：离线(offline) > 有活跃告警(warning) > 在线(online)。
    """
    result = await db.execute(select(Device).order_by(Device.id))
    devices = result.scalars().all()

    # 有活跃告警的设备 → 节点状态显示为 warning（黄色）
    alert_result = await db.execute(
        select(Alert.device_id).where(Alert.status == "active")
    )
    active_alert_device_ids = {row[0] for row in alert_result.all()}

    nodes = []
    for d in devices:
        if d.status == "offline":
            status = "offline"
        elif d.id in active_alert_device_ids:
            status = "warning"
        else:
            status = d.status or "online"
        nodes.append({
            "id": str(d.id),
            "name": d.name or d.ip,
            "ip": d.ip,
            "vendor": d.vendor or "",
            "type": d.device_type or "unknown",
            "status": status,
            "cpu": d.cpu_usage,
            "memory": d.memory_usage,
        })

    link_result = await db.execute(
        select(TopologyLink)
        .options(
            joinedload(TopologyLink.source_device),
            joinedload(TopologyLink.target_device),
        )
        .order_by(TopologyLink.id)
    )
    links = link_result.scalars().all()

    edges = []
    for link in links:
        edges.append({
            "id": f"link-{link.id}",
            "source": str(link.source_device_id),
            "target": str(link.target_device_id),
            "link_type": link.link_type,
            "label": link.label,
            "custom": True,
        })

    return {"code": 0, "message": "success", "data": {"nodes": nodes, "edges": edges}}


@router.get("/links")
async def list_links(db: AsyncSession = Depends(get_db)):
    """列出全部自定义连线（含设备名）。"""
    result = await db.execute(
        select(TopologyLink)
        .options(
            joinedload(TopologyLink.source_device),
            joinedload(TopologyLink.target_device),
        )
        .order_by(TopologyLink.id)
    )
    links = result.scalars().all()
    return [
        {
            "id": l.id,
            "source_device_id": l.source_device_id,
            "source_name": l.source_device.name if l.source_device else str(l.source_device_id),
            "target_device_id": l.target_device_id,
            "target_name": l.target_device.name if l.target_device else str(l.target_device_id),
            "link_type": l.link_type,
            "label": l.label,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        }
        for l in links
    ]


@router.post("/links", status_code=201)
async def create_link(data: TopologyLinkCreate, db: AsyncSession = Depends(get_db)):
    """创建自定义连线（校验设备存在、禁止自连、自动去重，source/target 规范化）。"""
    if data.source_device_id == data.target_device_id:
        raise HTTPException(status_code=400, detail="不能将设备连接到自身")

    # 规范化：小的 device_id 作为 source
    source_id, target_id = sorted([data.source_device_id, data.target_device_id])

    dev_ids = {source_id, target_id}
    result = await db.execute(select(Device.id).where(Device.id.in_(dev_ids)))
    found = {row[0] for row in result.all()}
    if found != dev_ids:
        raise HTTPException(status_code=404, detail="设备不存在")

    # 去重：同一对设备已存在连线则直接返回
    existing = await db.execute(
        select(TopologyLink).where(
            TopologyLink.source_device_id == source_id,
            TopologyLink.target_device_id == target_id,
        )
    )
    if existing.scalars().first() is not None:
        raise HTTPException(status_code=409, detail="这两个设备之间已存在连线")

    link = TopologyLink(
        source_device_id=source_id,
        target_device_id=target_id,
        link_type=data.link_type or "custom",
        label=data.label,
    )
    db.add(link)
    await db.commit()
    await db.refresh(link)
    return {"id": link.id, "source_device_id": link.source_device_id,
            "target_device_id": link.target_device_id, "link_type": link.link_type,
            "label": link.label}


@router.delete("/links/{link_id}", status_code=204)
async def delete_link(link_id: int, db: AsyncSession = Depends(get_db)):
    """删除自定义连线。"""
    result = await db.execute(select(TopologyLink).where(TopologyLink.id == link_id))
    link = result.scalar_one_or_none()
    if not link:
        raise HTTPException(status_code=404, detail="连线不存在")
    await db.delete(link)
    await db.commit()
    return None
