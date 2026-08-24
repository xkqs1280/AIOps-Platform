"""重要业务监控 API 路由：业务分组、监控终端 CRUD、状态查询、告警记录"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.business_monitor import BusinessGroup, BusinessTerminal, BusinessAlert
from app.schemas.device import _validate_ip

router = APIRouter(prefix="/business-monitor", tags=["重要业务监控"])


# ── Schemas ────────────────────────────────────────────────────────
class GroupCreate(BaseModel):
    name: str
    description: str | None = None


class GroupUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class TerminalCreate(BaseModel):
    group_id: int
    name: str
    ip: str
    mac: str | None = None
    description: str | None = None
    enabled: bool = True

    @field_validator("ip")
    @classmethod
    def _check_ip(cls, v: str) -> str:
        return _validate_ip(v)


class TerminalUpdate(BaseModel):
    group_id: int | None = None
    name: str | None = None
    ip: str | None = None
    mac: str | None = None
    description: str | None = None
    enabled: bool | None = None

    @field_validator("ip")
    @classmethod
    def _check_ip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_ip(v)


class TerminalBatchCreate(BaseModel):
    """批量添加终端：名称前缀 + 起始序号 + 起始 IP + 数量，自动生成递增序列。"""
    group_id: int
    name_prefix: str = Field(..., min_length=1, max_length=60)
    start_index: int = Field(1, ge=1)
    start_ip: str
    count: int = Field(..., ge=1, le=200)
    description: str | None = None

    @field_validator("start_ip")
    @classmethod
    def _check_start_ip(cls, v: str) -> str:
        return _validate_ip(v)


def _ip_add(ip: str, n: int) -> str | None:
    """IPv4 数值递增 n，越界返回 None。"""
    parts = ip.split(".")
    if len(parts) != 4:
        return None
    try:
        nums = [int(p) for p in parts]
        if any(x < 0 or x > 255 for x in nums):
            return None
    except ValueError:
        return None
    val = (nums[0] << 24) | (nums[1] << 16) | (nums[2] << 8) | nums[3]
    val += n
    if val > 0xFFFFFFFF:
        return None
    return f"{(val >> 24) & 255}.{(val >> 16) & 255}.{(val >> 8) & 255}.{val & 255}"


# ── 业务分组 ───────────────────────────────────────────────────────
@router.get("/groups")
async def list_groups(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(
            BusinessGroup,
            func.count(BusinessTerminal.id).label("terminal_count"),
        )
        .outerjoin(BusinessTerminal, BusinessTerminal.group_id == BusinessGroup.id)
        .group_by(BusinessGroup.id)
        .order_by(BusinessGroup.id)
    )
    items = []
    for g, cnt in result.all():
        items.append({
            "id": g.id,
            "name": g.name,
            "description": g.description,
            "terminal_count": cnt,
        })
    return {"items": items}


@router.post("/groups", status_code=201)
async def create_group(data: GroupCreate, db: AsyncSession = Depends(get_db)):
    exists = (await db.execute(
        select(BusinessGroup).where(BusinessGroup.name == data.name)
    )).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=409, detail="分组名称已存在")
    g = BusinessGroup(name=data.name, description=data.description)
    db.add(g)
    await db.commit()
    await db.refresh(g)
    return {"id": g.id, "name": g.name, "description": g.description, "terminal_count": 0}


@router.put("/groups/{group_id}")
async def update_group(group_id: int, data: GroupUpdate, db: AsyncSession = Depends(get_db)):
    g = (await db.execute(
        select(BusinessGroup).where(BusinessGroup.id == group_id)
    )).scalar_one_or_none()
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    if data.name is not None:
        g.name = data.name
    if data.description is not None:
        g.description = data.description
    await db.commit()
    return {"id": g.id, "name": g.name, "description": g.description}


@router.delete("/groups/{group_id}", status_code=204)
async def delete_group(group_id: int, db: AsyncSession = Depends(get_db)):
    g = (await db.execute(
        select(BusinessGroup).where(BusinessGroup.id == group_id)
    )).scalar_one_or_none()
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    await db.delete(g)  # terminals/alerts 级联删除
    await db.commit()
    return None


# ── 监控终端 ───────────────────────────────────────────────────────
def _term_dict(t: BusinessTerminal) -> dict:
    return {
        "id": t.id,
        "group_id": t.group_id,
        "name": t.name,
        "ip": t.ip,
        "mac": t.mac,
        "description": t.description,
        "enabled": t.enabled,
        "status": t.status,
        "last_check_at": t.last_check_at.isoformat() if t.last_check_at else None,
        "last_online_at": t.last_online_at.isoformat() if t.last_online_at else None,
        "last_offline_at": t.last_offline_at.isoformat() if t.last_offline_at else None,
        "offline_count": t.offline_count,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


@router.get("/terminals")
async def list_terminals(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=200),
    group_id: int | None = Query(None),
    status: str | None = Query(None),
    keyword: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(BusinessTerminal)
    count_stmt = select(func.count(BusinessTerminal.id))
    if group_id:
        stmt = stmt.where(BusinessTerminal.group_id == group_id)
        count_stmt = count_stmt.where(BusinessTerminal.group_id == group_id)
    if status:
        stmt = stmt.where(BusinessTerminal.status == status)
        count_stmt = count_stmt.where(BusinessTerminal.status == status)
    if keyword:
        kw = f"%{keyword}%"
        stmt = stmt.where(
            BusinessTerminal.name.ilike(kw) | BusinessTerminal.ip.ilike(kw) |
            BusinessTerminal.description.ilike(kw)
        )
        count_stmt = count_stmt.where(
            BusinessTerminal.name.ilike(kw) | BusinessTerminal.ip.ilike(kw) |
            BusinessTerminal.description.ilike(kw)
        )
    total = (await db.execute(count_stmt)).scalar()
    offset = (page - 1) * page_size
    result = await db.execute(stmt.order_by(BusinessTerminal.id.desc()).offset(offset).limit(page_size))
    terminals = result.scalars().all()
    return {"total": total, "items": [_term_dict(t) for t in terminals]}


@router.post("/terminals", status_code=201)
async def create_terminal(data: TerminalCreate, db: AsyncSession = Depends(get_db)):
    g = (await db.execute(
        select(BusinessGroup).where(BusinessGroup.id == data.group_id)
    )).scalar_one_or_none()
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    t = BusinessTerminal(
        group_id=data.group_id,
        name=data.name,
        ip=data.ip,
        mac=data.mac,
        description=data.description,
        enabled=data.enabled,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return _term_dict(t)


@router.post("/terminals/batch", status_code=201)
async def batch_create_terminals(data: TerminalBatchCreate, db: AsyncSession = Depends(get_db)):
    """批量添加监控终端：名称前缀+起始序号+起始IP+数量，自动生成递增序列。

    如 name_prefix=AP, start_index=1, start_ip=192.168.1.50, count=50
    → AP_1(192.168.1.50) ~ AP_50(192.168.1.99)。已存在的 IP 自动跳过。
    """
    g = (await db.execute(
        select(BusinessGroup).where(BusinessGroup.id == data.group_id)
    )).scalar_one_or_none()
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")

    # 已存在的终端 IP（避免重复）
    existing_rows = (await db.execute(select(BusinessTerminal.ip))).scalars().all()
    existing_ips = set(existing_rows)

    created = []
    skipped = 0
    for i in range(data.count):
        ip = _ip_add(data.start_ip, i)
        if ip is None:
            raise HTTPException(
                status_code=400,
                detail=f"IP 地址 {data.start_ip} 递增 {i} 台后超出 IPv4 范围，请调整起始 IP 或数量",
            )
        if ip in existing_ips:
            skipped += 1
            continue
        name = f"{data.name_prefix}_{data.start_index + i}"
        t = BusinessTerminal(
            group_id=data.group_id,
            name=name,
            ip=ip,
            description=data.description,
            enabled=True,
        )
        db.add(t)
        existing_ips.add(ip)
        created.append(t)
    await db.commit()
    for t in created:
        await db.refresh(t)

    return {"created": len(created), "skipped": skipped, "items": [_term_dict(t) for t in created]}


@router.put("/terminals/{terminal_id}")
async def update_terminal(terminal_id: int, data: TerminalUpdate, db: AsyncSession = Depends(get_db)):
    t = (await db.execute(
        select(BusinessTerminal).where(BusinessTerminal.id == terminal_id)
    )).scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="终端不存在")
    if data.group_id is not None:
        t.group_id = data.group_id
    if data.name is not None:
        t.name = data.name
    if data.ip is not None:
        t.ip = data.ip
    if data.mac is not None:
        t.mac = data.mac
    if data.description is not None:
        t.description = data.description
    if data.enabled is not None:
        t.enabled = data.enabled
    await db.commit()
    await db.refresh(t)
    return _term_dict(t)


@router.delete("/terminals/{terminal_id}", status_code=204)
async def delete_terminal(terminal_id: int, db: AsyncSession = Depends(get_db)):
    t = (await db.execute(
        select(BusinessTerminal).where(BusinessTerminal.id == terminal_id)
    )).scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="终端不存在")
    await db.delete(t)
    await db.commit()
    return None


# ── 手动探测 ───────────────────────────────────────────────────────
@router.post("/terminals/{terminal_id}/probe")
async def probe_terminal(terminal_id: int, db: AsyncSession = Depends(get_db)):
    """立即手动探测单台终端（不等 5 分钟轮询）。"""
    from app.services.business_monitor_service import _probe_single

    t = (await db.execute(
        select(BusinessTerminal).where(BusinessTerminal.id == terminal_id)
    )).scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="终端不存在")
    await _probe_single(db, t)
    await db.refresh(t)
    return _term_dict(t)


@router.post("/terminals/probe-all")
async def probe_all(db: AsyncSession = Depends(get_db)):
    """立即手动探测全部启用终端。"""
    from app.services.business_monitor_service import _probe_single

    result = await db.execute(
        select(BusinessTerminal).where(BusinessTerminal.enabled == True)  # noqa: E712
    )
    terminals = result.scalars().all()
    for t in terminals:
        await _probe_single(db, t)
    return {"probed": len(terminals)}


# ── 告警记录 ───────────────────────────────────────────────────────
@router.get("/alerts")
async def list_alerts(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    terminal_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(BusinessAlert)
    count_stmt = select(func.count(BusinessAlert.id))
    if terminal_id:
        stmt = stmt.where(BusinessAlert.terminal_id == terminal_id)
        count_stmt = count_stmt.where(BusinessAlert.terminal_id == terminal_id)
    total = (await db.execute(count_stmt)).scalar()
    offset = (page - 1) * page_size
    result = await db.execute(
        stmt.order_by(BusinessAlert.id.desc()).offset(offset).limit(page_size)
    )
    alerts = result.scalars().all()
    return {
        "total": total,
        "items": [
            {
                "id": a.id,
                "terminal_id": a.terminal_id,
                "terminal_name": a.terminal_name,
                "terminal_ip": a.terminal_ip,
                "alert_type": a.alert_type,
                "severity": a.severity,
                "message": a.message,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in alerts
        ],
    }


@router.delete("/alerts/{alert_id}", status_code=204)
async def delete_alert(alert_id: int, db: AsyncSession = Depends(get_db)):
    """删除单条业务监控告警记录。"""
    alert = (
        await db.execute(select(BusinessAlert).where(BusinessAlert.id == alert_id))
    ).scalar_one_or_none()
    if not alert:
        raise HTTPException(status_code=404, detail="告警记录不存在")
    await db.delete(alert)
    await db.commit()
    return None


@router.delete("/alerts")
async def clear_alerts(db: AsyncSession = Depends(get_db)):
    """一键清空全部业务监控告警记录。返回清理数量。

    同时重置离线终端的告警状态机：清空后仍离线的终端，将在下一轮探测达到
    阈值时重新生成离线告警记录（否则 status 已为 offline，去重条件
    status != "offline" 会阻止再次记录）。
    """
    count = (
        await db.execute(select(func.count(BusinessAlert.id)))
    ).scalar() or 0
    await db.execute(BusinessAlert.__table__.delete())
    from app.services.business_monitor_service import OFFLINE_ALERT_THRESHOLD
    await db.execute(
        BusinessTerminal.__table__.update()
        .where(BusinessTerminal.status == "offline")
        .values(status="unknown", offline_count=OFFLINE_ALERT_THRESHOLD - 1, online_count=0)
    )
    await db.commit()
    return {"deleted": count}


@router.get("/summary")
async def summary(db: AsyncSession = Depends(get_db)):
    """概览统计：总数/在线/离线/告警数。"""
    total = (await db.execute(select(func.count(BusinessTerminal.id)))).scalar() or 0
    online = (await db.execute(
        select(func.count(BusinessTerminal.id)).where(BusinessTerminal.status == "online")
    )).scalar() or 0
    offline = (await db.execute(
        select(func.count(BusinessTerminal.id)).where(BusinessTerminal.status == "offline")
    )).scalar() or 0
    unknown = max(0, total - online - offline)
    recent_offline = (await db.execute(
        select(func.count(BusinessAlert.id)).where(BusinessAlert.alert_type == "offline")
    )).scalar() or 0
    return {
        "total": total, "online": online, "offline": offline, "unknown": unknown,
        "offline_alerts": recent_offline,
    }
