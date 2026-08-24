from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
import asyncio
import logging

from app.database import get_db
from app.models.device import Device
from app.models.alert import Alert
from app.services.credential_service import reveal_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["监控大屏"])


@router.get("/overview")
async def dashboard_overview(db: AsyncSession = Depends(get_db)):
    # 设备状态统计
    total_result = await db.execute(select(func.count(Device.id)))
    total_devices = total_result.scalar() or 0

    online_result = await db.execute(select(func.count(Device.id)).where(Device.status == "online"))
    online = online_result.scalar() or 0

    offline_result = await db.execute(select(func.count(Device.id)).where(Device.status == "offline"))
    offline = offline_result.scalar() or 0

    warning_result = await db.execute(select(func.count(Device.id)).where(Device.status == "warning"))
    warning = warning_result.scalar() or 0

    # 活跃告警数（status='active'，与「告警管理」页活跃口径一致）
    active_alerts_result = await db.execute(
        select(func.count(Alert.id)).where(Alert.status == "active")
    )
    active_alerts = active_alerts_result.scalar() or 0

    # 按类型分布
    type_result = await db.execute(
        select(Device.device_type, func.count(Device.id)).group_by(Device.device_type)
    )
    type_distribution = {row.device_type or "unknown": row.count for row in type_result.all()}

    # 按厂商分布
    vendor_result = await db.execute(
        select(Device.vendor, func.count(Device.id)).group_by(Device.vendor)
    )
    vendor_distribution = {row.vendor or "unknown": row.count for row in vendor_result.all()}

    return {
        "code": 0,
        "message": "success",
        "data": {
            "total_devices": total_devices,
            "online": online,
            "offline": offline,
            "warning": warning,
            "active_alerts": active_alerts,
            "type_distribution": type_distribution,
            "vendor_distribution": vendor_distribution,
        },
    }


@router.get("/cpu-ranking")
async def cpu_ranking(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Device.name, Device.ip, Device.cpu_usage, Device.vendor)
        .where(Device.cpu_usage.isnot(None))
        .order_by(Device.cpu_usage.desc())
        .limit(5)
    )
    return {
        "code": 0,
        "message": "success",
        "data": [
            {"name": r.name, "ip": r.ip, "cpu_usage": r.cpu_usage, "vendor": r.vendor}
            for r in result.all()
        ],
    }


@router.get("/memory-ranking")
async def memory_ranking(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Device.name, Device.ip, Device.memory_usage, Device.vendor)
        .where(Device.memory_usage.isnot(None))
        .order_by(Device.memory_usage.desc())
        .limit(5)
    )
    return {
        "code": 0,
        "message": "success",
        "data": [
            {"name": r.name, "ip": r.ip, "memory_usage": r.memory_usage, "vendor": r.vendor}
            for r in result.all()
        ],
    }


@router.get("/bandwidth-ranking")
async def bandwidth_ranking(db: AsyncSession = Depends(get_db)):
    """带宽利用率 TOP10：真实 SNMP 接口流量（每台设备取最大利用率接口）。

    并发限流采集，避免大量设备时拖垮接口；不可达/未开放 MIB 的设备自动跳过。
    返回项含设备名、IP、最大利用率、对应接口名及上下行速率。
    """
    from app.services.interface_traffic_service import collect_interface_traffic

    result = await db.execute(select(Device))
    devices = result.scalars().all()

    sem = asyncio.Semaphore(12)  # 控制并发 snmpwalk 子进程数

    async def _collect(d: Device) -> dict | None:
        async with sem:
            try:
                ifaces = await collect_interface_traffic(
                    d.ip, community=reveal_secret(d.snmp_community) or "aiops",
                    sample_interval=3.0, timeout=4,
                )
            except Exception as e:
                logger.debug(f"bandwidth collect failed {d.name}({d.ip}): {type(e).__name__}")
                return None
        if not ifaces:
            return None
        top = max(ifaces, key=lambda x: x["max_util"])
        return {
            "name": d.name,
            "ip": d.ip,
            "vendor": d.vendor,
            "bandwidth_usage": top["max_util"],
            "interface": top["name"],
            "in_util": top["in_util"],
            "out_util": top["out_util"],
            "in_rate": top["in_rate"],
            "out_rate": top["out_rate"],
        }

    results = [r for r in await asyncio.gather(*(_collect(d) for d in devices)) if r]
    results.sort(key=lambda x: x["bandwidth_usage"], reverse=True)
    return {
        "code": 0,
        "message": "success",
        "data": results[:10],
    }


@router.get("/lifecycle")
async def lifecycle_reminders(db: AsyncSession = Depends(get_db)):
    """设备生命周期提醒：即将过保/维保到期"""
    result = await db.execute(select(Device).order_by(Device.id))
    devices = result.scalars().all()

    reminders = []
    for d in devices:
        if d.warranty_expire:
            reminders.append({
                "device_name": d.name,
                "type": "过保提醒",
                "date": d.warranty_expire.isoformat() if d.warranty_expire else None,
                "severity": "warning",
            })
        if d.eos_date:
            reminders.append({
                "device_name": d.name,
                "type": "维保到期",
                "date": d.eos_date.isoformat() if d.eos_date else None,
                "severity": "minor",
            })

    return {"code": 0, "message": "success", "data": reminders[:10]}


@router.get("/recent-alerts")
async def recent_alerts(db: AsyncSession = Depends(get_db)):
    """最近活跃告警（大屏滚动用）+ 活跃告警概览统计。

    口径：仅 status='active' 的告警，与「告警管理」页面筛选「活跃」完全一致，
    保证大屏「设备健康概览-活跃告警」「活跃告警概览」与告警管理数字同步。
    """
    result = await db.execute(
        select(Alert)
        .options(joinedload(Alert.device))
        .where(Alert.status == "active")
        .order_by(Alert.triggered_at.desc())
        .limit(20)
    )
    alerts = result.scalars().all()

    # 活跃告警级别分布（单独 count，不受 limit 截断影响）
    sev_result = await db.execute(
        select(Alert.severity, func.count(Alert.id))
        .where(Alert.status == "active")
        .group_by(Alert.severity)
    )
    summary = {"total": 0, "active": 0, "critical": 0, "major": 0, "minor": 0, "warning": 0}
    for row in sev_result.all():
        sev = row.severity
        if sev in summary:
            summary[sev] = row.count
            summary["total"] += row.count
            summary["active"] += row.count

    return {
        "code": 0,
        "message": "success",
        "summary": summary,
        "data": [
            {
                "id": a.id,
                "device_name": a.device.name if a.device else "",
                "severity": a.severity,
                "message": a.message,
                "status": a.status,
                "triggered_at": a.triggered_at.isoformat() if a.triggered_at else None,
            }
            for a in alerts
        ],
    }
