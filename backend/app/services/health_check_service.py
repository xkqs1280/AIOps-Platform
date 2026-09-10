"""设备可达性检测服务 - 每5秒探测设备存活状态（SNMP + ping 双重探测），连续3次双失败判定离线。

离线判定策略：
  - SNMP 正常 → 在线；
  - SNMP 无响应但 ping 可达 → 网络通、仅 SNMP 异常，标 warning 不计离线；
  - SNMP 与 ping 均无响应 → 计入失败，连续 3 次双失败判定离线。
"""
import asyncio
import logging
import subprocess
import sys
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device
from app.models.alert import Alert
from app.services.discovery_service import snmp_get
from app.services.credential_service import reveal_secret
from app.services.notify_service import dispatch_alert

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
SNMP_RULE_NAME = "SNMP 采集异常"

# 内存中的失败计数器: {device_id: fail_count}
_failure_counts: dict[int, int] = {}


async def _ping_ok(ip: str) -> bool | None:
    """异步 ping 探测（跨平台），收到 TTL 响应即视为可达。

    - Windows: `ping -n <count> -w <ms>`（Linux 的 -n/-w 语义不同，需区分）
    - Linux:   `ping -c <count> -W <sec>`

    返回三态，将「设备无响应」与「探测机制异常」区分开：
    - True ：设备可达（收到 ICMP 回复）
    - False：ping 命令正常执行但无回复（确认不可达）
    - None ：探测机制异常（子进程启动失败 / 超时 / IO 错误）——上层必须跳过
             本轮失败计数，避免把「探测不可用」误判为「设备不可达」。

    背景（生产故障 2026-09-07）：旧实现把 ping 子进程的一切异常静默按 False
    处理，在 Windows 打包进程（计划任务/隐藏窗口会话）下 ping 探测实际从未
    成功过——任何 SNMP 瞬时失败都被当作「SNMP+ping 双失败」，连续 3 次即误报
    离线（外部 ping 全程可达），全网刷「设备不可达」。异常改为记录日志并返回
    None，既能暴露真实原因，也不再产生误报。
    """
    if sys.platform == "win32":
        cmd = ["ping", "-n", "2", "-w", "2000", ip]
        # 无控制台会话（服务/计划任务/隐藏启动）下 spawn console 子进程需显式
        # CREATE_NO_WINDOW，避免新建控制台窗口或启动失败
        create_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc_kwargs: dict = {"creationflags": create_flags}
    else:
        cmd = ["ping", "-c", "2", "-W", "2", ip]
        proc_kwargs = {}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **proc_kwargs,
        )
    except Exception as e:
        logger.warning(f"Ping probe spawn failed for {ip}: {e!r}")
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        logger.warning(f"Ping probe timeout for {ip}")
        try:
            proc.kill()
        except Exception:
            pass
        return None
    except Exception as e:
        logger.warning(f"Ping probe error for {ip}: {e!r}")
        try:
            proc.kill()
        except Exception:
            pass
        return None
    return out.decode("utf-8", errors="replace").upper().count("TTL=") > 0


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
        await _resolve_snmp_alerts(db, device)
    else:
        # SNMP 无响应 → ping 二次确认：双失败才计入离线判定
        ping_ok = await _ping_ok(device.ip)
        if ping_ok is None:
            # ping 探测机制异常（子进程启动失败/超时等）：不能据此判定设备离线。
            # 跳过本轮计数并记日志——曾致生产环境外部 ping 全程可达却全网误报
            # 「设备不可达」（旧版将探测异常静默按 False 累计）。
            logger.warning(
                f"Ping probe unavailable for {device.name}({device.ip}), "
                f"skip offline counting this round"
            )
            return
        if ping_ok:
            # ping 可达：网络通、设备在线。SNMP 异常（community 错/UDP 被禁/
            # agent 停止）不再覆盖在线状态，改用去重的 warning 告警表达。
            _failure_counts[device.id] = 0
            if device.status != "online":
                logger.info(
                    f"Device {device.name}({device.ip}) -> online "
                    f"(ping ok, SNMP no response)"
                )
            device.status = "online"
            device.last_seen = datetime.now(timezone.utc)
            # 网络已可达，此前若存在离线告警（如旧逻辑误判）应恢复
            await _resolve_offline_alerts(db, device)
            await _ensure_snmp_alert(db, device)
            return

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
            # 每次探测到离线都尝试确保告警存在（内部去做重 + 5 分钟防抖）。
            # 不再只在「状态刚转变」时调用：上游恢复时被抑制的离线告警会被解除，
            # 若设备其实仍在离线，仅靠状态转换门将永远无法重新生成告警。
            await _ensure_offline_alert(db, device)
            device.status = "offline"
        # 1~2 次失败：可能是瞬时抖动，不降级状态（保持 online），
        # 连续 3 次双失败才判定离线，避免状态闪烁


async def _ensure_snmp_alert(db: AsyncSession, device: Device):
    """ping 可达但 SNMP 无响应：生成去重的 warning 告警（状态保持在线）。

    采集异常是告警信息，不覆盖设备在线状态；SNMP 恢复后由
    _resolve_snmp_alerts 自动关闭。

    抑制策略（与离线告警一致的防抖窗口）：设备 SNMP 间歇性恢复会导致
    「创建→恢复→再创建」反复横跳刷屏（如测试服 SW2 曾在 24h 内产生 90 条），
    因此 DEBOUNCE_MINUTES 窗口内即使已恢复过也不再重复创建告警。
    """
    existing = await db.execute(
        select(Alert).where(
            Alert.device_id == device.id,
            Alert.rule_name == SNMP_RULE_NAME,
            Alert.status == "active",
        )
    )
    if existing.scalars().first() is not None:
        return
    # 防抖：DEBOUNCE_MINUTES 内该设备已触发过 SNMP 采集异常告警（含已恢复）→ 抑制，
    # 避免 SNMP 间歇恢复导致的反复横跳刷屏（与 offline 告警防抖语义一致）
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=DEBOUNCE_MINUTES)
    recent = await db.execute(
        select(Alert).where(
            Alert.device_id == device.id,
            Alert.rule_name == SNMP_RULE_NAME,
            Alert.triggered_at >= cutoff,
        )
    )
    if recent.scalars().first() is not None:
        logger.info(
            f"Suppress SNMP alert for {device.name}({device.ip}): "
            f"re-triggered within {DEBOUNCE_MINUTES}min debounce window"
        )
        return
    alert = Alert(
        device_id=device.id,
        rule_name=SNMP_RULE_NAME,
        severity="warning",
        message=(
            f"设备 {device.name}({device.ip}) ping 可达但 SNMP 无响应，"
            f"请检查 community 配置 / UDP 161 放行 / SNMP agent 状态"
        ),
        status="active",
        triggered_at=datetime.now(timezone.utc),
    )
    db.add(alert)
    logger.warning(f"Created SNMP-unreachable alert for {device.name}({device.ip})")
    from app.services.alert_suppressor import record_alert_created
    record_alert_created(device.id)
    await dispatch_alert(
        db,
        device_name=device.name, device_ip=device.ip,
        rule_name=SNMP_RULE_NAME, severity="warning",
        message=alert.message, dedup_key=f"snmp:{device.id}",
    )


async def _resolve_snmp_alerts(db: AsyncSession, device: Device):
    """SNMP 恢复响应后，关闭该设备所有 active 的 SNMP 采集异常告警"""
    result = await db.execute(
        select(Alert).where(
            Alert.device_id == device.id,
            Alert.rule_name == SNMP_RULE_NAME,
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
        logger.info(f"Resolved {resolved} SNMP alert(s) for {device.name}({device.ip})")


async def _ensure_offline_alert(db: AsyncSession, device: Device):
    """若该设备尚无 active 的离线告警，则创建一条 critical 告警（去重 + 防抖，避免反复弹）。

    额外经过拓扑依赖抑制：若本设备的上游已不可达，则本次离线很可能是失去上联的连带
    结果，标记为 suppressed（仍然落库可查），避免一台核心设备掉线刷出全网离线告警。
    """
    existing = await db.execute(
        select(Alert).where(
            Alert.device_id == device.id,
            Alert.rule_name == OFFLINE_RULE_NAME,
            Alert.status.in_(("active", "suppressed")),
        )
    )
    existing_row = existing.scalars().first()
    # 防抖：DEBOUNCE_MINUTES 内该设备已触发过离线告警（含已恢复）→ 抑制，避免反复横跳刷屏
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=DEBOUNCE_MINUTES)
    recent = await db.execute(
        select(Alert).where(
            Alert.device_id == device.id,
            Alert.rule_name == OFFLINE_RULE_NAME,
            Alert.triggered_at >= cutoff,
        )
    )
    recent_row = recent.scalars().first()

    from app.services.alert_suppressor import (
        build_dependency_context,
        evaluate_suppression,
        record_alert_created,
    )

    try:
        dep_ctx = await build_dependency_context(db)
    except Exception as e:
        logger.warning("build dependency context failed in health check: %s", e)
        dep_ctx = None

    decision = evaluate_suppression(device, OFFLINE_RULE_NAME, "critical", dep_ctx)

    if existing_row is not None:
        # 已存在：若此前被依赖抑制、现在上游已恢复，则升级为活动告警并补发通知
        if existing_row.status == "suppressed" and not decision["suppressed"]:
            existing_row.status = "active"
            existing_row.suppress_reason = None
            existing_row.suppressed_by_device_id = None
            existing_row.triggered_at = datetime.now(timezone.utc)
            logger.warning(f"Offline alert un-suppressed for {device.name}({device.ip})")
            await dispatch_alert(
                db,
                device_name=device.name, device_ip=device.ip,
                rule_name=OFFLINE_RULE_NAME, severity="critical",
                message=existing_row.message, dedup_key=f"offline:{device.id}",
            )
        return

    if recent_row is not None and not decision["suppressed"]:
        logger.info(
            f"Suppress offline alert for {device.name}({device.ip}): "
            f"re-triggered within {DEBOUNCE_MINUTES}min debounce window"
        )
        return

    message = (
        f"设备 {device.name}({device.ip}) 不可达："
        f"SNMP 与 ping 均连续 {MAX_FAILURES} 次无响应，判定为离线"
    )
    alert = Alert(
        device_id=device.id,
        rule_name=OFFLINE_RULE_NAME,
        severity="critical",
        message=message,
        status="suppressed" if decision["suppressed"] else "active",
        triggered_at=datetime.now(timezone.utc),
        suppressed_by_device_id=decision["by_device_id"],
        suppress_reason=decision["reason"],
    )
    db.add(alert)
    record_alert_created(device.id)

    if decision["suppressed"]:
        logger.info(
            f"Offline alert suppressed for {device.name}({device.ip}): {decision['reason']}"
        )
        return

    logger.warning(f"Created offline alert for {device.name}({device.ip})")
    await dispatch_alert(
        db,
        device_name=device.name, device_ip=device.ip,
        rule_name=OFFLINE_RULE_NAME, severity="critical",
        message=message, dedup_key=f"offline:{device.id}",
        notify=decision["notify"],
    )


async def _resolve_offline_alerts(db: AsyncSession, device: Device):
    """将设备所有 active 的离线告警标记为已恢复（resolved）"""
    result = await db.execute(
        select(Alert).where(
            Alert.device_id == device.id,
            Alert.rule_name == OFFLINE_RULE_NAME,
            Alert.status.in_(("active", "suppressed")),
        )
    )
    now = datetime.now(timezone.utc)
    resolved = 0
    suppressed_only = True
    for alert in result.scalars().all():
        if alert.status == "active":
            suppressed_only = False
        alert.status = "resolved"
        alert.resolved_at = now
        resolved += 1
    if resolved:
        logger.info(f"Resolved {resolved} offline alert(s) for {device.name}({device.ip})")
        from app.services.alert_suppressor import record_resolution
        record_resolution(device.id, OFFLINE_RULE_NAME)
        if suppressed_only:
            # 仅解除了被依赖抑制的告警：设备此前并未真正"恢复在线"的对外告警，
            # 不发恢复通知，避免误导值班人员以为故障已解除。
            return
        from app.services.notify_service import dispatch_alert
        try:
            await dispatch_alert(
                db,
                device_name=device.name, device_ip=device.ip,
                rule_name=OFFLINE_RULE_NAME, severity="critical",
                message=(
                    f"设备 {device.name}({device.ip}) 已恢复正常，"
                    f"SNMP 与 ping 探测均可达。"
                ),
                dedup_key=f"recover:{device.id}",
            )
        except Exception as e:
            logger.warning(f"recover alert notify failed: {e}")


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
    logger.info("Device health check service started (interval=5s, probe=snmp+ping, max_failures=3)")

    while True:
        try:
            await run_health_check()
        except Exception as e:
            logger.error(f"Health check loop error: {e}")
        await asyncio.sleep(CHECK_INTERVAL)
