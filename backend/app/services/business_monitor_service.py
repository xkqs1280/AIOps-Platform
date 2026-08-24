"""重要业务监控服务：ping 探测终端在线状态，离线自动告警。

设计：
- 每 5 分钟轮询一次启用中的监控终端；
- 每台终端 ping 3 次，3 次全部不可达判定该轮次离线；
- 连续离线达到阈值（默认 2 次轮询 = 10 分钟）才触发离线告警，避免网络抖动误报；
- 恢复在线（连续探测可达）时自动写入恢复告警。
"""
import asyncio
import logging
import subprocess
import sys

from sqlalchemy import select

from app.database import async_session

logger = logging.getLogger(__name__)

# 轮询间隔（秒）
PROBE_INTERVAL = 5 * 60  # 5 分钟
# ping 次数
PING_COUNT = 3
# 触发离线告警的连续离线轮询次数（2 次 = 10 分钟）
OFFLINE_ALERT_THRESHOLD = 2
# 并发探测上限
MAX_PARALLEL = 20
# 同类告警防抖窗口（秒）：离线/恢复反复翻转 5 分钟内不重复告警
TERMINAL_DEBOUNCE_SECONDS = 300
# 最近同类告警时间: (terminal_id, alert_type) -> datetime
_last_terminal_alert: dict = {}


def _ping_ip(ip: str) -> bool:
    """ping 探测（跨平台），返回是否可达（TTL 出现次数 > 0）。

    - Windows: `ping -n <count> -w <ms>`（Linux 的 -n/-w 语义不同，需区分）
    - Linux:   `ping -c <count> -W <sec>`
    """
    try:
        if sys.platform == "win32":
            cmd = ["ping", "-n", str(PING_COUNT), "-w", "2000", ip]
        else:
            cmd = ["ping", "-c", str(PING_COUNT), "-W", "2", ip]
        proc = subprocess.run(
            cmd, capture_output=True, timeout=PING_COUNT * 3 + 3,
        )
        output = proc.stdout.decode("utf-8", errors="replace") + proc.stderr.decode("utf-8", errors="replace")
        return output.upper().count("TTL=") > 0
    except Exception:
        return False


async def probe_terminal_online(ip: str) -> bool:
    """异步探测终端在线状态（线程池跑同步 ping）。"""
    return await asyncio.to_thread(_ping_ip, ip)


async def _probe_single(db, terminal) -> None:
    """探测单台终端并更新状态/触发告警。"""
    from datetime import datetime, timezone
    from app.models.business_monitor import BusinessAlert

    now = datetime.now(timezone.utc)
    reachable = await probe_terminal_online(terminal.ip)
    prev_status = terminal.status

    def _debounced(alert_type: str) -> bool:
        """同类告警防抖：5 分钟内不重复生成（抑制离线/恢复反复翻转刷屏）。"""
        key = (terminal.id, alert_type)
        last = _last_terminal_alert.get(key)
        if last and (now - last).total_seconds() < TERMINAL_DEBOUNCE_SECONDS:
            return True
        _last_terminal_alert[key] = now
        return False

    if reachable:
        terminal.online_count += 1
        terminal.offline_count = 0
        terminal.status = "online"
        terminal.last_online_at = now
        if prev_status == "offline" and not _debounced("recovered"):
            db.add(BusinessAlert(
                terminal_id=terminal.id,
                terminal_name=terminal.name,
                terminal_ip=terminal.ip,
                alert_type="recovered",
                severity="info",
                message=f"终端 {terminal.name} ({terminal.ip}) 已恢复在线",
            ))
            logger.info(f"Business terminal recovered: {terminal.name} ({terminal.ip})")
    else:
        terminal.offline_count += 1
        terminal.online_count = 0
        terminal.last_offline_at = now
        # 达到阈值且尚未标记离线 → 触发离线告警（带防抖）
        if terminal.offline_count >= OFFLINE_ALERT_THRESHOLD and terminal.status != "offline" and not _debounced("offline"):
            terminal.status = "offline"
            minutes = terminal.offline_count * PROBE_INTERVAL // 60
            db.add(BusinessAlert(
                terminal_id=terminal.id,
                terminal_name=terminal.name,
                terminal_ip=terminal.ip,
                alert_type="offline",
                severity="critical",
                message=f"终端 {terminal.name} ({terminal.ip}) 连续离线约 {minutes} 分钟，疑似断电/断线/设备故障",
            ))
            logger.warning(f"Business terminal offline: {terminal.name} ({terminal.ip})")

    terminal.last_check_at = now
    await db.commit()


async def probe_terminals_loop() -> None:
    """后台循环：每 PROBE_INTERVAL 轮询所有启用终端。"""
    from app.models.business_monitor import BusinessTerminal

    while True:
        try:
            async with async_session() as db:
                result = await db.execute(
                    select(BusinessTerminal).where(BusinessTerminal.enabled == True)  # noqa: E712
                )
                terminals = result.scalars().all()

                sem = asyncio.Semaphore(MAX_PARALLEL)

                async def _limited(t):
                    async with sem:
                        async with async_session() as tdb:
                            # 重新加载，避免跨会话使用 detached 对象
                            cur = (
                                await tdb.execute(
                                    select(BusinessTerminal).where(BusinessTerminal.id == t.id)
                                )
                            ).scalar_one_or_none()
                            if cur:
                                await _probe_single(tdb, cur)

                if terminals:
                    await asyncio.gather(*[_limited(t) for t in terminals])
                    offline = (
                        await db.execute(
                            select(BusinessTerminal)
                            .where(BusinessTerminal.status == "offline")
                        )
                    ).scalars().all()
                    logger.info(
                        f"Business probe round done: {len(terminals)} terminals, "
                        f"{len(offline)} offline"
                    )
        except Exception:
            logger.exception("Business monitor probe loop error")
        await asyncio.sleep(PROBE_INTERVAL)


async def start_business_monitor() -> asyncio.Task:
    """启动业务监控后台轮询任务。"""
    task = asyncio.create_task(probe_terminals_loop())
    logger.info("Business monitor service started")
    return task
