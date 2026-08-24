"""真实指标采集服务 - 通过 SNMP 采集设备 CPU/内存/温度（替代原模拟值）。

实现说明：
- 内存：HUAWEI-MEMORY-MIB (1.3.6.1.4.1.2011.6.3.5.1.1) 真实可用，4 台设备全部支持。
  聚合所有内存池的 used(col2)/free(col3) 字节数，计算真实使用率。
- CPU：HUAWEI-CPU-MIB / hwEntityCpu 在本环境 S5700/AR 的 VRP 版本 SNMP 代理未实现
  （返回 No Such Object 或全 0），此时存 None，前端显示「设备未开放该 MIB」。
- 温度：hwEntityTemperature 同样未实现，存 None。
所有采集均来自设备真实 SNMP 响应，无任何硬编码/模拟值。
"""
import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, delete

from app.models.device import Device
from app.services.credential_service import reveal_secret
from app.services.discovery_service import snmp_walk as discovery_snmp_walk
from app.models.metric_record import MetricRecord

logger = logging.getLogger(__name__)

# HUAWEI-MEMORY-MIB :: hwMemTable
MEMORY_OID = "1.3.6.1.4.1.2011.6.3.5.1.1"
# HUAWEI-CPU-MIB :: hwCpuUsage5Sec（实测 S5700/AR 返回真实利用率，如 2% / 13%）
CPU_5SEC_OID = "1.3.6.1.4.1.2011.6.3.4.1.3"
# HUAWEI-CPU-MIB :: hwCpuDevUsage（部分设备可用，作回退）
CPU_DEV_USAGE_OID = "1.3.6.1.4.1.2011.6.3.4.1.2"
# HUAWEI-CPU-MIB :: hwCpuTable（多数设备未实现，作回退）
CPU_MIB_OID = "1.3.6.1.4.1.2011.6.1.1.1"
# HUAWEI-ENTITY-EXTENT-MIB :: hwEntityCpuUsage（多返回 0）
ENTITY_CPU_OID = "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.5"
# HUAWEI-ENTITY-EXTENT-MIB :: hwEntityTemperature（多返回 0）
ENTITY_TEMP_OID = "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.11"

# H3C Comware (HH3C-ENTITY-EXT-MIB) - 实测 5 台 H3C 设备均可返回真实值
H3C_CPU_OID = "1.3.6.1.4.1.25506.2.6.1.1.1.1.6"    # hh3cEntityExtCpuUsage
H3C_MEM_OID = "1.3.6.1.4.1.25506.2.6.1.1.1.1.8"    # hh3cEntityExtMemUsage
H3C_TEMP_OID = "1.3.6.1.4.1.25506.2.6.1.1.1.1.12"  # hh3cEntityExtTemperature

COLLECT_INTERVAL = 60          # 采集间隔（秒）
SNMP_TIMEOUT = 5              # 单设备 SNMP 超时
MAX_CONCURRENT = 10           # 并发采集上限
RETENTION_DAYS = 30           # 时序数据保留天数


async def snmp_walk(ip: str, oid: str, community: str = "aiops", timeout: int = SNMP_TIMEOUT):
    """snmpwalk 指定 OID 子树，返回 [(full_oid, value_str), ...]；失败返回 []。"""
    return await discovery_snmp_walk(ip, oid, community, timeout=timeout)


def _column_of(oid_str: str, base: str):
    """返回 oid_str 在 base OID 之后的第一列号（int），不匹配返回 None。"""
    oid_str = oid_str.lstrip(".")
    prefix = base.lstrip(".") + "."
    if not oid_str.startswith(prefix):
        return None
    rem = oid_str[len(prefix):]
    parts = rem.split(".")
    try:
        return int(parts[0])
    except (ValueError, IndexError):
        return None


async def collect_memory(ip: str, community: str):
    """采集真实内存使用率(%)。返回 (pct_or_None, used_bytes_or_None)。"""
    rows = await snmp_walk(ip, MEMORY_OID, community)
    used = 0.0
    free = 0.0
    for oid, val in rows:
        col = _column_of(oid, MEMORY_OID)
        try:
            v = float(val)
        except (ValueError, TypeError):
            continue
        if col == 2:      # hwMemUsed
            used += v
        elif col == 3:    # hwMemFree
            free += v
    if used + free <= 0:
        return None, None
    pct = used / (used + free) * 100.0
    return round(pct, 1), round(used)


async def _collect_scalar_max(ip: str, oids: list[str], community: str):
    """依次尝试多个 OID，返回所有实例中的最大值；若全 0/空则 None（视为设备未开放）。"""
    vals: list[float] = []
    for oid in oids:
        rows = await snmp_walk(ip, oid, community)
        cur: list[float] = []
        for _, val in rows:
            try:
                cur.append(float(val))
            except (ValueError, TypeError):
                continue
        if cur:
            vals = cur
            # 若首个返回的 OID 全为 0/无效值，继续尝试下一个候选 OID
            mx = max(vals)
            if mx > 0:
                break
    if not vals:
        return None
    mx = max(vals)
    if mx <= 0:
        return None
    return round(mx, 1)


async def collect_cpu(ip: str, community: str):
    return await _collect_scalar_max(
        ip, [CPU_5SEC_OID, CPU_DEV_USAGE_OID, CPU_MIB_OID, ENTITY_CPU_OID], community,
    )


async def collect_temperature(ip: str, community: str):
    return await _collect_scalar_max(ip, [ENTITY_TEMP_OID], community)


def _to_float(val: str):
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _h3c_entity_max(rows: list[tuple[str, str]], max_ok: float, exclude_zero: bool):
    """H3C 实体表取有效最大值。

    H3C 实体扩展表（CPU/内存/温度）对未实现该指标的实体返回 0 或
    哨兵值 65535(不支持)。需剔除这些后再取真实最大值：
    - 65535：固定哨兵，直接忽略
    - 0（仅 CPU/内存）：多数为"该实体无 CPU/内存"，忽略
    - > max_ok：异常/哨兵，忽略
    """
    candidates = []
    for _, val in rows:
        v = _to_float(val)
        if v is None or v == 65535 or v > max_ok:
            continue
        if exclude_zero and v <= 0:
            continue
        candidates.append(v)
    return round(max(candidates), 1) if candidates else None


async def collect_h3c_cpu(ip: str, community: str):
    rows = await snmp_walk(ip, H3C_CPU_OID, community)
    return _h3c_entity_max(rows, max_ok=100, exclude_zero=True)


async def collect_h3c_memory(ip: str, community: str):
    """H3C 内存使用率(%)：hh3cEntityExtMemUsage 直接给出百分比，取有效最大值。"""
    rows = await snmp_walk(ip, H3C_MEM_OID, community)
    return (_h3c_entity_max(rows, max_ok=100, exclude_zero=True), None)


async def collect_h3c_temperature(ip: str, community: str):
    """H3C 温度：hh3cEntityExtTemperature，剔除 65535 哨兵后取有效最大值(0~120℃)。"""
    rows = await snmp_walk(ip, H3C_TEMP_OID, community)
    return _h3c_entity_max(rows, max_ok=120, exclude_zero=False)


async def collect_device_metrics(db, device: Device) -> dict:
    """采集单台设备的真实指标并写入 Device 字段与 metric_records 时序表。"""
    community = reveal_secret(device.snmp_community) or "aiops"
    vendor = (device.vendor or "")
    is_h3c = ("H3C" in vendor.upper()) or ("COMWARE" in vendor.upper()) or ("华三" in vendor)

    if is_h3c:
        mem_pct, mem_raw = await collect_h3c_memory(device.ip, community)
        cpu = await collect_h3c_cpu(device.ip, community)
        temp = await collect_h3c_temperature(device.ip, community)
    else:
        mem_pct, mem_raw = await collect_memory(device.ip, community)
        cpu = await collect_cpu(device.ip, community)
        temp = await collect_temperature(device.ip, community)

    # 采集失败（None）时保留旧值，避免瞬时超时把指标清空导致告警误恢复
    if mem_pct is not None:
        device.memory_usage = mem_pct
    if cpu is not None:
        device.cpu_usage = cpu
    if temp is not None:
        device.temperature = temp

    now = datetime.now(timezone.utc)
    if mem_pct is not None:
        db.add(MetricRecord(device_id=device.id, metric_type="memory",
                            value=mem_pct, raw_value=mem_raw, recorded_at=now))
    if cpu is not None:
        db.add(MetricRecord(device_id=device.id, metric_type="cpu",
                            value=cpu, recorded_at=now))
    if temp is not None:
        db.add(MetricRecord(device_id=device.id, metric_type="temperature",
                            value=temp, recorded_at=now))

    logger.info(
        f"Metrics {device.name}({device.ip}): mem={mem_pct}% cpu={cpu} temp={temp}"
    )
    return {"memory": mem_pct, "cpu": cpu, "temperature": temp}


async def _cleanup_old_records():
    """清理超过保留期的时序数据。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    from app.database import async_session
    try:
        async with async_session() as db:
            await db.execute(delete(MetricRecord).where(MetricRecord.recorded_at < cutoff))
            await db.commit()
        logger.info(f"Cleaned metric_records older than {RETENTION_DAYS}d")
    except Exception as e:
        logger.error(f"Metric cleanup error: {e}")


async def metrics_collect_loop():
    """后台指标采集循环 - 每 60 秒采集一次全设备真实指标。"""
    await asyncio.sleep(15)
    logger.info(f"Metrics collector started (interval={COLLECT_INTERVAL}s)")
    iteration = 0
    while True:
        try:
            from app.database import async_session
            async with async_session() as db:
                result = await db.execute(select(Device))
                devices = result.scalars().all()
                if devices:
                    sem = asyncio.Semaphore(MAX_CONCURRENT)

                    async def _collect(d):
                        async with sem:
                            try:
                                return await collect_device_metrics(db, d)
                            except Exception as e:
                                logger.error(f"Metrics error {d.name}({d.ip}): {e}")
                                return None

                    await asyncio.gather(*[_collect(d) for d in devices])
                    await db.commit()

                    # 采集完成后立即评估告警规则（状态库：SNMP 指标变化 -> 触发/恢复告警）
                    try:
                        from app.services.alert_rule_engine import evaluate_rules
                        await evaluate_rules(db, devices)
                    except Exception as e:
                        logger.error(f"Alert rule evaluate error: {e}")

            iteration += 1
            if iteration % 60 == 0:   # 约每小时清理一次
                await _cleanup_old_records()
        except Exception as e:
            logger.error(f"Metrics collect loop error: {e}")
        await asyncio.sleep(COLLECT_INTERVAL)
