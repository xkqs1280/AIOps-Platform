"""设备生命周期管理服务：厂商EOS/EOL数据管理与生命周期提醒"""
from datetime import datetime, timedelta, date

from sqlalchemy import select, and_, or_, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.p2_baseline import LifecycleDB
from app.models.device import Device
from app.database import get_session


async def get_lifecycle_db(session, vendor=None, search=None):
    """查询生命周期数据库，可按厂商过滤或按型号模糊搜索"""
    stmt = select(LifecycleDB)

    conditions = []
    if vendor:
        conditions.append(LifecycleDB.vendor == vendor)
    if search:
        conditions.append(LifecycleDB.model.ilike(f"%{search}%"))

    if conditions:
        stmt = stmt.where(and_(*conditions))

    stmt = stmt.order_by(LifecycleDB.vendor, LifecycleDB.model)
    result = await session.execute(stmt)
    return result.scalars().all()


async def add_lifecycle_entry(session, vendor, model, eos_date, eol_date, source="manual"):
    """添加新的生命周期条目"""
    entry = LifecycleDB(
        vendor=vendor,
        model=model,
        eos_date=eos_date,
        eol_date=eol_date,
        source=source,
    )
    session.add(entry)
    await session.flush()
    await session.refresh(entry)
    return entry


async def update_lifecycle_entry(session, entry_id, **kwargs):
    """更新生命周期条目，支持字段：eos_date, eol_date, eos_announce, source"""
    stmt = select(LifecycleDB).where(LifecycleDB.id == entry_id)
    result = await session.execute(stmt)
    entry = result.scalars().first()

    if not entry:
        return None

    allowed_fields = {"eos_date", "eol_date", "eos_announce", "source"}
    for key, value in kwargs.items():
        if key in allowed_fields:
            setattr(entry, key, value)

    entry.updated_at = datetime.utcnow()
    await session.flush()
    return entry


async def delete_lifecycle_entry(session, entry_id):
    """根据ID删除生命周期条目，返回是否删除成功"""
    stmt = select(LifecycleDB).where(LifecycleDB.id == entry_id)
    result = await session.execute(stmt)
    entry = result.scalars().first()

    if not entry:
        return False

    await session.delete(entry)
    await session.flush()
    return True


async def match_device_lifecycle(session, device_id):
    """根据设备的厂商和型号匹配生命周期数据库，自动填充设备生命周期字段"""
    device_stmt = select(Device).where(Device.id == device_id)
    device_result = await session.execute(device_stmt)
    device = device_result.scalars().first()

    if not device or not device.vendor or not device.model:
        return None

    lc_stmt = select(LifecycleDB).where(
        and_(
            LifecycleDB.vendor == device.vendor,
            LifecycleDB.model == device.model,
        )
    )
    lc_result = await session.execute(lc_stmt)
    lifecycle_entry = lc_result.scalars().first()

    if not lifecycle_entry:
        return None

    if lifecycle_entry.eos_date:
        device.eos_date = datetime.combine(lifecycle_entry.eos_date, datetime.min.time())
        # 估算保修到期日：以停止销售日期作为保修到期参考
        device.warranty_expire = datetime.combine(lifecycle_entry.eos_date, datetime.min.time())
    if lifecycle_entry.eol_date:
        device.eol_date = datetime.combine(lifecycle_entry.eol_date, datetime.min.time())

    await session.flush()
    return lifecycle_entry


async def get_lifecycle_reminders(session):
    """扫描所有设备，生成生命周期提醒"""
    today = date.today()

    reminders = {
        "warranty_expiring": [],
        "eos_approaching": [],
        "eol_approaching": [],
        "eol_critical": [],
        "already_eol": [],
    }

    stmt = select(Device)
    result = await session.execute(stmt)
    devices = result.scalars().all()

    for device in devices:
        # 检查保修到期（≤90天）
        if device.warranty_expire:
            warranty_date = (
                device.warranty_expire.date()
                if isinstance(device.warranty_expire, datetime)
                else device.warranty_expire
            )
            days_remaining = (warranty_date - today).days
            if 0 <= days_remaining <= 90:
                reminders["warranty_expiring"].append({
                    "device_name": device.name,
                    "device_id": device.id,
                    "date": warranty_date.isoformat(),
                    "days_remaining": days_remaining,
                    "severity": "warning",
                })

        # 检查EOS临近（≤180天）
        if device.eos_date:
            eos_date = (
                device.eos_date.date()
                if isinstance(device.eos_date, datetime)
                else device.eos_date
            )
            days_remaining = (eos_date - today).days
            if 0 <= days_remaining <= 180:
                reminders["eos_approaching"].append({
                    "device_name": device.name,
                    "device_id": device.id,
                    "date": eos_date.isoformat(),
                    "days_remaining": days_remaining,
                    "severity": "warning",
                })

        # 检查EOL状态
        if device.eol_date:
            eol_date = (
                device.eol_date.date()
                if isinstance(device.eol_date, datetime)
                else device.eol_date
            )
            days_remaining = (eol_date - today).days

            if days_remaining < 0:
                # 已过EOL日期
                reminders["already_eol"].append({
                    "device_name": device.name,
                    "device_id": device.id,
                    "date": eol_date.isoformat(),
                    "days_remaining": days_remaining,
                    "severity": "critical",
                })
            elif days_remaining <= 180:
                # EOL紧急（≤180天），同时属于EOL临近
                reminders["eol_critical"].append({
                    "device_name": device.name,
                    "device_id": device.id,
                    "date": eol_date.isoformat(),
                    "days_remaining": days_remaining,
                    "severity": "critical",
                })
                reminders["eol_approaching"].append({
                    "device_name": device.name,
                    "device_id": device.id,
                    "date": eol_date.isoformat(),
                    "days_remaining": days_remaining,
                    "severity": "critical",
                })
            elif days_remaining <= 365:
                # EOL临近（≤365天）
                reminders["eol_approaching"].append({
                    "device_name": device.name,
                    "device_id": device.id,
                    "date": eol_date.isoformat(),
                    "days_remaining": days_remaining,
                    "severity": "warning",
                })

    return reminders


async def seed_lifecycle_data(session):
    """插入初始生命周期数据（厂商常见型号的EOS/EOL信息），已存在则跳过"""
    seed_entries = [
        # H3C
        {"vendor": "H3C", "model": "S5560-EI", "eos_date": date(2024, 12, 31), "eol_date": date(2027, 12, 31)},
        {"vendor": "H3C", "model": "S6800", "eos_date": date(2025, 6, 30), "eol_date": date(2028, 6, 30)},
        {"vendor": "H3C", "model": "MSR3620", "eos_date": date(2023, 12, 31), "eol_date": date(2026, 12, 31)},
        # 华为
        {"vendor": "华为", "model": "S5720", "eos_date": date(2024, 6, 30), "eol_date": date(2027, 6, 30)},
        {"vendor": "华为", "model": "CE6800", "eos_date": date(2025, 12, 31), "eol_date": date(2028, 12, 31)},
        {"vendor": "华为", "model": "AR6300", "eos_date": date(2024, 3, 31), "eol_date": date(2027, 3, 31)},
        # 深信服
        {"vendor": "深信服", "model": "NGAF-1000", "eos_date": date(2025, 6, 30), "eol_date": date(2028, 6, 30)},
    ]

    for entry_data in seed_entries:
        stmt = pg_insert(LifecycleDB).values(
            vendor=entry_data["vendor"],
            model=entry_data["model"],
            eos_date=entry_data["eos_date"],
            eol_date=entry_data["eol_date"],
            source="manual",
        )
        stmt = stmt.on_conflict_do_nothing(index_elements=["vendor", "model"])
        await session.execute(stmt)

    await session.flush()
