"""设备可达性检测服务 - 每5秒SNMP探测设备存活状态，连续3次无响应判定离线"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device
from app.models.alert import Alert
from app.services.discovery_service import snmp_get
from app.services.credential_service import reveal_secret

logger = logging.getLogger(__name__)

tz_8 = timezone(timedelta(hours=8))

# sysUpTime OID - 设备启动时间，几乎所有SNMP设备都支持
SYS_UPTIME_OID = "1.3.6.1.2.1.1.3.0"

# 探测间隔（秒）
CHECK_INTERVAL = 5

# 连续失败多少次判定为离线
MAX_FAILURES = 3

# SNMP 超时（秒）
SNMP_TIMEOUT = 3

# 每轮并发探测的信号量限制
MAX_CONCURRENT = 20

# 离线告警防抖窗口（分钟）：同一设备离线告警触发后，窗口内即使恢复再次离线也不重复弹，
# 抑制"离线→恢复→离线"反复横跳导致的告警刷屏
DEBOUNCE_MINUTES = 5

# 离线告警规则名（与种子数据中的「设备不可达」告警规则保持一致）
OFFLINE_RULE_NAME = "设备不可达"

# 内存中的失败计数器: {device_id: fail_count}
_failure_counts: dict[int, int] = {}


async def _check_single_device(db: AsyncSession, device: Device):
    """探测单台设备并更新状态，必要时生成/恢复离线告警"""
    community = reveal_secret(device.snmp_community) or "aiops"
    result = await snmp_get(device.ip, SYS_UPTIME_OID, community, timeout=SNMP_TIMEOUT)

    if result is not None:
        # 探测成功：重置计数器，标记在线
        _failure_counts[device.id] = 0
        if device.status != "online":
            logger.info(f"Device {device.name}({device.ip}) -> online")
        device.status = "online"
        device.last_seen = datetime.now(timezone.utc)
        # 只要探测成功就尝试恢复离线告警（幂等，不依赖状态转换）。
        # 原因：设备状态可能已被其他路径改为 online 或进程重启导致状态与
        # 告警不一致，若仅依赖 was_offline 转换则 active 告警永远不会被恢复。
        await _resolve_offline_alerts(db, device)
    else:
        # 探测失败：递增计数器
        current = _failure_counts.get(device.id, 0) + 1
        _failure_counts[device.id] = current

        if current >= MAX_FAILURES:
            # 连续3次失败：标记离线
            was_offline = device.status == "offline"
            if not was_offline:
                logger.warning(
                    f"Device {device.name}({device.ip}) -> offline "
                    f"(failed {current} consecutive checks)"
                )
                # 状态刚转变为离线：生成不可达告警（自动去重）
                await _ensure_offline_alert(db, device)
            device.status = "offline"
        else:
            # 1~2次失败：标记告警（降级状态）
            if device.status == "online" or device.status == "unknown":
                logger.info(
                    f"Device {device.name}({device.ip}) -> warning "
                    f"(failed {current}/{MAX_FAILURES})"
                )
                device.status = "warning"


async def _ensure_offline_alert(db: AsyncSession, device: Device):
    """若该设备尚无 active 的离线告警，则创建一条 critical 告警（去重 + 防抖，避免反复弹）"""
    existing = await db.execute(
        select(Alert).where(
            Alert.device_id == device.id,
            Alert.rule_name == OFFLINE_RULE_NAME,
            Alert.status == "active",
        )
    )
    if existing.scalars().first() is not None:
        return
    # 防抖：DEBOUNCE_MINUTES 内该设备已触发过离线告警（含已恢复）→ 抑制，避免反复横跳刷屏
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=DEBOUNCE_MINUTES)
    recent = await db.execute(
        select(Alert).where(
            Alert.device_id == device.id,
            Alert.rule_name == OFFLINE_RULE_NAME,
            Alert.triggered_at >= cutoff,
        )
    )
    if recent.scalars().first() is not None:
        logger.info(
            f"Suppress offline alert for {device.name}({device.ip}): "
            f"re-triggered within {DEBOUNCE_MINUTES}min debounce window"
        )
        return
    alert = Alert(
        device_id=device.id,
        rule_name=OFFLINE_RULE_NAME,
        severity="critical",
        message=(
            f"设备 {device.name}({device.ip}) 不可达："
            f"SNMP 已连续 {MAX_FAILURES} 次无响应，判定为离线"
        ),
        status="active",
        triggered_at=datetime.now(timezone.utc),
    )
    db.add(alert)
    logger.warning(f"Created offline alert for {device.name}({device.ip})")
    # 邮件告警（异步发送，失败不影响业务；同设备 5 分钟防轰炸窗口）
    from app.services.mail_service import send_alert_email
    try:
        await send_alert_email(
            db,
            subject=f"[严重] 设备离线 - {device.name}",
            body=(
                f"告警时间：{datetime.now(tz_8).strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"设备名称：{device.name}\n"
                f"IP 地址：{device.ip}\n"
                f"严重级别：严重\n"
                f"告警内容：设备不可达，SNMP 已连续 {MAX_FAILURES} 次无响应，判定为离线\n\n"
                f"—— AIOps 智能运维托管平台"
            ),
            dedup_key=f"offline:{device.id}",
        )
    except Exception as e:
        logger.warning(f"offline alert email failed: {e}")


async def _resolve_offline_alerts(db: AsyncSession, device: Device):
    """将设备所有 active 的离线告警标记为已恢复（resolved）"""
    result = await db.execute(
        select(Alert).where(
            Alert.device_id == device.id,
            Alert.rule_name == OFFLINE_RULE_NAME,
            Alert.status == "active",
        )
    )
    now = datetime.now(timezone.utc)
    resolved = 0
    for alert in result.scalars().all():
        alert.status = "resolved"
        alert.resolved_at = now
        resolved += 1
    if resolved:
        logger.info(f"Resolved {resolved} offline alert(s) for {device.name}({device.ip})")
        # 邮件通知恢复（异步发送，失败不影响业务；同设备 5 分钟防轰炸窗口）
        from app.services.mail_service import send_alert_email
        try:
            await send_alert_email(
                db,
                subject=f"[恢复] 设备恢复在线 - {device.name}",
                body=(
                    f"恢复时间：{now.astimezone(tz_8).strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"设备名称：{device.name}\n"
                    f"IP 地址：{device.ip}\n"
                    f"设备已恢复正常。\n\n"
                    f"—— AIOps 智能运维托管平台"
                ),
                dedup_key=f"recover:{device.id}",
            )
        except Exception as e:
            logger.warning(f"recover alert email failed: {e}")


async def run_health_check():
    """执行一轮全设备可达性检测"""
    from app.database import async_session

    async with async_session() as db:
        result = await db.execute(select(Device))
        devices = result.scalars().all()

        if not devices:
            return

        sem = asyncio.Semaphore(MAX_CONCURRENT)

        async def _check_with_sem(device):
            async with sem:
                try:
                    await _check_single_device(db, device)
                except Exception as e:
                    logger.error(f"Health check error for {device.name}({device.ip}): {e}")

        # 并发探测所有设备
        await asyncio.gather(*[_check_with_sem(d) for d in devices])

        # 统一提交状态变更
        await db.commit()


async def health_check_loop():
    """后台健康检测循环 - 每5秒执行一轮"""
    # 启动后等待10秒，确保DB就绪
    await asyncio.sleep(10)
    logger.info("Device health check service started (interval=5s, max_failures=3)")

    while True:
        try:
            await run_health_check()
        except Exception as e:
            logger.error(f"Health check loop error: {e}")
        await asyncio.sleep(CHECK_INTERVAL)
