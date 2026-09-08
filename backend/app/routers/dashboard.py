from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
import asyncio
import logging

from app.database import get_db, async_session
from app.models.device import Device
from app.models.alert import Alert
from app.services.credential_service import reveal_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["监控大屏"])

# ---- 带宽利用率 TOP10 缓存（stale-while-revalidate）----
# 实时 SNMP 双采样较慢（约 10-30s），不能每次请求都现场采集：
# 首次采集入缓存，60s 内直接返回；过期后返回旧数据并后台刷新，请求永不阻塞。
_BW_CACHE: dict = {"data": [], "ts": 0.0}
_BW_TASK: asyncio.Task | None = None
_BW_TTL = 60  # 缓存有效期（秒）
_BW_MIN_INTERVAL = 20  # 两次后台刷新最小间隔（秒），防止请求风暴叠加采集


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
async def bandwidth_ranking():
    """带宽利用率 TOP10：真实 SNMP 接口流量（每台设备取最大利用率接口）。

    带缓存（stale-while-revalidate）：首次现场采集（约 10-30s），之后 60s 内
    直接命中缓存秒回；过期后立即返回旧数据并触发后台刷新，前端轮询不再被
    采集耗时阻塞。不可达/未开放 MIB 的设备自动跳过。
    """
    import time as _time

    now = _time.time()
    age = now - _BW_CACHE["ts"]

    if _BW_CACHE["ts"] > 0:
        # 有缓存：立即返回（可能略旧），过期则后台刷新
        if age >= _BW_TTL and age >= _BW_MIN_INTERVAL:
            _spawn_bw_refresh()
        return {"code": 0, "message": "success", "data": _BW_CACHE["data"][:10]}

    # 首次：现场采集（仅这一次会让请求等待）
    await _collect_bandwidth()
    return {"code": 0, "message": "success", "data": _BW_CACHE["data"][:10]}


def _spawn_bw_refresh():
    """启动后台刷新任务（单飞：已有进行中的任务则跳过）。"""
    global _BW_TASK
    if _BW_TASK is not None and not _BW_TASK.done():
        return
    _BW_TASK = asyncio.create_task(_bg_collect_bandwidth())


async def _bg_collect_bandwidth():
    """后台刷新：使用独立数据库会话（请求会话此时已关闭）。"""
    try:
        async with async_session() as db:
            await _collect_bandwidth(db)
    except Exception as e:
        logger.debug(f"bandwidth background refresh failed: {type(e).__name__}: {e}")


async def _collect_bandwidth(db: AsyncSession | None = None):
    """采集全部设备接口流量并写入 _BW_CACHE。"""
    import time as _time
    from app.services.interface_traffic_service import collect_interface_traffic

    if db is None:
        async with async_session() as db2:
            return await _collect_bandwidth(db2)

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
        # 若该设备的最大利用率接口是 LACP 聚合成员口（正常情况已被服务层剔除，
        # 此处兜底），跳过该设备，避免聚合成员以聚合层流量虚高上榜。
        if top.get("lacp_member"):
            return None
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

    results = [r for r in await asyncio.gather(*(_collect(d) for d in devices), return_exceptions=True) if isinstance(r, dict)]
    results.sort(key=lambda x: x["bandwidth_usage"], reverse=True)
    _BW_CACHE["data"] = results
    _BW_CACHE["ts"] = _time.time()


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
