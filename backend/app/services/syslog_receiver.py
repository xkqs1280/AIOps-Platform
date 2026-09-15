"""内置 syslog UDP 接收器（设备日志中心 P0）。

架构背景（为什么必须内置）：
    平台此前所有"接收类"能力都是 HTTP 端点 + 外部转发组件。Trap 模块的注释写明
    "接收 snmptrapd 转发"，而**部署包里根本没有转发组件** —— 也就是说 Trap 与 syslog
    在生产上都没有真实入口。设备只会往 UDP 514 发 syslog，不会往 HTTP 端点 POST，
    所以在后端进程内直接监听 UDP 才是"开箱即用、一键部署"的正确解法。

可靠性设计：
    - **受监督**：由 ``loop_supervisor.start_supervised`` 托管，崩溃/意外退出 15 秒后自动重启；
    - **批量入库**：内存队列攒批（默认 500 条或 2 秒），绝不做"一条一 commit"——
      这是千万条量级下唯一可行的写入方式；
    - **限流可观测**：按来源 IP 限流，队列溢出与限流丢弃分别计数，不静默丢包；
    - **不丢数据优先**：解析失败不抛异常，退化为"正文即整条报文"照常入库。

端口：514 为 syslog 标准端口。Linux 下 <1024 需 ``CAP_NET_BIND_SERVICE``
（install.sh 生成的 systemd unit 已加 ``AmbientCapabilities``），Windows 可直接绑定。
端口被占用或权限不足时**明确记日志**（含排查指引），并由监督器持续重试，同时通过
``receiver_status()`` 暴露给前端，避免出现"设备在发、平台静默收不到"的隐形故障。
"""
import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from time import monotonic

from sqlalchemy import insert

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 进程级状态
# ---------------------------------------------------------------------------

# 待入库队列：元素为 (source_ip, raw_text, received_at)
_queue: asyncio.Queue = asyncio.Queue(maxsize=settings.SYSLOG_QUEUE_MAX)

_stats: dict = {
    "received": 0,          # 收到的报文数（含被限流/溢出的）
    "stored": 0,            # 成功入库的日志条数
    "security_routed": 0,   # 额外分流到安全事件表的条数
    "dropped_overflow": 0,  # 队列满丢弃
    "dropped_rate": 0,      # 超限流阈值丢弃
    "store_errors": 0,      # 入库失败次数
    "last_received_at": None,
    "last_error": None,
    "bound_port": None,
    "bind_error": None,
    "started_at": None,
}

_last_flush = monotonic()
_flush_running = False

# 按来源 IP 限流（防设备环路/广播风暴把数据库和前端一起拖死）
_rate_buckets: dict[str, deque] = {}
_MAX_RATE_KEYS = 5_000

# IP → (device_id, vendor) 缓存：避免每条日志查一次库
_device_cache: dict[str, tuple[int | None, str | None]] = {}
_device_cache_at = 0.0
_DEVICE_CACHE_TTL = 60.0

# 单轮落盘最多处理的条数（防止一轮占用过久，影响其它后台循环）
_MAX_ROWS_PER_CYCLE = 20_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def reset_state() -> None:
    """把接收器恢复到初始状态（测试用）。"""
    global _queue, _flush_running, _last_flush, _device_cache_at
    # **重建队列**而不是只清空：用例可能为模拟溢出把它换成小容量队列，
    # 只清空不复位容量会让后续用例默默按小容量丢弃日志（曾导致连锁失败）。
    _queue = asyncio.Queue(maxsize=settings.SYSLOG_QUEUE_MAX)
    for k in _stats:
        if k in ("received", "stored", "security_routed", "dropped_overflow",
                 "dropped_rate", "store_errors"):
            _stats[k] = 0
        else:
            _stats[k] = None
    _rate_buckets.clear()
    # 设备缓存**时间戳必须一并清零**：只清缓存字典不清时间戳的话，下一次
    # ``_resolve_device`` 会认为缓存仍然新鲜（60s TTL 内）而跳过刷新，于是所有
    # 日志都关联不上设备（device_id 全为 NULL）。生产环境表现为"重启后一段时间
    # 内日志不关联设备"，测试环境表现为用例互相污染。
    _device_cache.clear()
    _device_cache_at = 0.0
    _last_flush = monotonic()
    _flush_running = False


def receiver_status() -> dict:
    """接收器运行状态（供 API/前端展示）。"""
    from app.services.loop_supervisor import heartbeats

    beat = heartbeats().get("syslog-udp")
    return {
        "enabled": settings.SYSLOG_UDP_ENABLED,
        "running": beat is not None and (monotonic() - beat) < 30,
        "host": settings.SYSLOG_UDP_HOST,
        "port": settings.SYSLOG_UDP_PORT,
        "bound_port": _stats["bound_port"],
        "bind_error": _stats["bind_error"],
        "queue_size": _queue.qsize(),
        "queue_max": settings.SYSLOG_QUEUE_MAX,
        "received": _stats["received"],
        "stored": _stats["stored"],
        "security_routed": _stats["security_routed"],
        "dropped_overflow": _stats["dropped_overflow"],
        "dropped_rate": _stats["dropped_rate"],
        "store_errors": _stats["store_errors"],
        "last_received_at": _stats["last_received_at"],
        "last_error": _stats["last_error"],
        "started_at": _stats["started_at"],
        "device_tz_offset_hours": settings.SYSLOG_DEVICE_TZ_OFFSET_HOURS,
        # 留存天数（.env 的 DEVICE_LOGS_DAYS，默认 180）。前端副标题要显示**实际生效值**，
        # 不能硬编码 —— 否则客户在 .env 调大后，页面还在展示旧数字（"文案与实现不一致"）。
        "retention_days": settings.DEVICE_LOGS_DAYS,
    }


# ---------------------------------------------------------------------------
# 设备缓存
# ---------------------------------------------------------------------------

async def _refresh_device_cache() -> None:
    """从 devices 表刷新 IP → (id, vendor) 缓存。

    设备日志是高频写入，不能每条都查库；未纳管设备发来的日志会以 device_id=None
    入库（保留数据，同时可作为"有设备没纳管"的发现线索）。
    """
    global _device_cache_at
    from sqlalchemy import select
    from app.database import async_session
    from app.models.device import Device

    try:
        async with async_session() as db:
            rows = (await db.execute(select(Device.ip, Device.id, Device.vendor))).all()
        _device_cache.clear()
        for ip, did, vendor in rows:
            if ip:
                _device_cache[ip] = (did, vendor)
        _device_cache_at = monotonic()
    except Exception as e:  # 数据库不可用时不应拖垮接收器
        logger.warning("刷新设备缓存失败：%s", e)


async def _resolve_device(source_ip: str | None) -> tuple[int | None, str | None]:
    if not source_ip:
        return None, None
    if (monotonic() - _device_cache_at) > _DEVICE_CACHE_TTL:
        await _refresh_device_cache()
    return _device_cache.get(source_ip, (None, None))


# ---------------------------------------------------------------------------
# 限流
# ---------------------------------------------------------------------------

def _rate_allowed(source_ip: str) -> bool:
    """按来源 IP 的滑动窗口限流。超限返回 False（并计入 dropped_rate）。"""
    window = settings.SYSLOG_RATE_WINDOW_SECONDS
    limit = settings.SYSLOG_RATE_MAX_PER_WINDOW
    if window <= 0 or limit <= 0:
        return True
    now = monotonic()
    dq = _rate_buckets.get(source_ip)
    if dq is None:
        # 桶数上限保护：随机逐出（日志风暴时来源 IP 可能很多）
        if len(_rate_buckets) >= _MAX_RATE_KEYS:
            for k in list(_rate_buckets.keys())[: _MAX_RATE_KEYS // 4]:
                _rate_buckets.pop(k, None)
        dq = deque()
        _rate_buckets[source_ip] = dq
    cutoff = now - window
    while dq and dq[0] <= cutoff:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


# ---------------------------------------------------------------------------
# UDP 协议实现
# ---------------------------------------------------------------------------

def _decode_payload(raw: bytes) -> str:
    """解码报文字节（UTF-8 优先，GBK 兜底）。

    设备日志常含中文（如描述性文本），H3C 传统编码为 GBK；固定 utf-8 会抛异常。

    同时**剥掉 NUL(0x00)**：真机 H3C 报文尾部常带 0x00 填充，
    而 PostgreSQL 的 text/varchar 不允许存 0x00，带进去会让批量 INSERT 整批失败
    （一条坏报文毁掉整批最多 500 条日志）。这里在入口就清掉，是最省事也最彻底的位置。
    """
    text = None
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    return text.replace("\x00", "") if "\x00" in text else text


def handle_datagram(data: bytes, source_ip: str, log_source: str = "udp") -> int:
    """处理一个 UDP 报文（同步，供协议回调与测试调用）。

    一个报文可能含多行（部分转发组件会合并），按行拆分为多条日志。

    Args:
        log_source: 入口来源标记（udp / http），落库到 ``device_logs.log_source``。

    Returns:
        实际入队（后续会入库）的条数。
    """
    _stats["received"] += 1
    _stats["last_received_at"] = _now().isoformat()
    max_bytes = settings.SYSLOG_MAX_MESSAGE_BYTES
    text = _decode_payload(data[:max_bytes] if max_bytes > 0 else data)

    queued = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if not _rate_allowed(source_ip):
            _stats["dropped_rate"] += 1
            continue
        try:
            _queue.put_nowait((source_ip, line, _now(), log_source))
            queued += 1
        except asyncio.QueueFull:
            _stats["dropped_overflow"] += 1
    return queued


class SyslogUDPProtocol(asyncio.DatagramProtocol):
    """UDP syslog 接收协议。"""

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            handle_datagram(data, addr[0] if addr else "unknown")
        except Exception as e:  # 单包异常不允许影响接收循环
            _stats["last_error"] = f"{type(e).__name__}: {e}"
            logger.warning("处理 syslog 报文异常（来自 %s）：%s", addr, e)

    def error_received(self, exc) -> None:  # pragma: no cover - 依赖真实网络栈
        _stats["last_error"] = f"UDP error: {exc}"
        logger.warning("syslog UDP 接收错误：%s", exc)


# ---------------------------------------------------------------------------
# 解析 + 批量落盘
# ---------------------------------------------------------------------------

async def build_rows(
    items: list[tuple[str, str, datetime, str]]
) -> tuple[list[dict], list[bool]]:
    """把 (来源 IP, 原始报文, 接收时间, 入口来源) 解析为 device_logs 行字典。

    Returns:
        (行字典列表, 逐行是否安全类的标记)——安全类需额外分流到 security_events。
        解析只做一次，标记随行返回，避免重复解析。
    """
    from app.services.device_log_service import parse_device_log, is_security_log

    offset = settings.SYSLOG_DEVICE_TZ_OFFSET_HOURS
    rows: list[dict] = []
    security_flags: list[bool] = []
    for source_ip, raw, received_at, log_source in items:
        parsed = parse_device_log(raw, now=received_at, tz_offset_hours=offset)
        device_id, _vendor = await _resolve_device(source_ip)
        rows.append({
            "device_id": device_id,
            "hostname": parsed["hostname"],
            "module": parsed["module"],
            "mnemonic": parsed["mnemonic"],
            "severity": parsed["severity"],
            "severity_name": parsed["severity_name"],
            "category": parsed["category"],
            "interface": parsed["interface"],
            "username": parsed["username"],
            # src_ip 的语义是「这条日志从哪台机器发来的」（UDP 源 / ingest 声明），
            # 不能被正文里提到的地址覆盖：H3C 的 "logged in from 10.0.0.9" 指的是
            # 登录发起方，而华为的 "OID 1.3.6.1.4.1.2011..." 一度被误抓成 1.3.6.1。
            # 正文里的地址仍完整保留在 content 中，不影响审计取证。
            "src_ip": source_ip,
            "content": parsed["content"],
            "device_time": parsed["device_time"],
            "device_time_raw": parsed["device_time_raw"],
            "received_at": received_at,
            "raw_log": parsed["raw_log"],
            "log_source": log_source,
        })
        security_flags.append(is_security_log(parsed))
    return rows, security_flags


async def _persist(items: list[tuple[str, str, datetime, str]]) -> int:
    """解析并批量写入 device_logs；安全类**额外**分流一份到 security_events。

    为什么安全类也进 device_logs：设备日志中心是审计留存表，完整性比"分类纯度"更重要
    （等保要看的是"网络日志留存"）。安全类同时写 security_events，是为了让既有安全面板
    继续可用 —— 即"一个入口、两条落库路径"的超集。

    容错：批量 INSERT 失败时**逐条重试**，避免一条畸形记录（如含 NUL、超长）把整批
    500 条好日志一起带走。审计表宁可慢一点，也不能整批丢。
    """
    from app.database import async_session
    from app.models.device_log import DeviceLog

    rows, security_flags = await build_rows(items)

    stored = 0
    lost = 0
    async with async_session() as db:
        try:
            await db.execute(insert(DeviceLog), rows)
            stored = len(rows)
        except Exception as e:
            logger.warning("设备日志批量入库失败（%d 条），转为逐条重试：%s", len(rows), e)
            await db.rollback()
            for row in rows:
                try:
                    await db.execute(insert(DeviceLog), [row])
                    await db.commit()
                    stored += 1
                except Exception as e2:
                    await db.rollback()
                    lost += 1
                    _stats["last_error"] = f"row insert: {type(e2).__name__}: {e2}"
                    logger.error("单条设备日志入库失败（已丢弃该条）：%s | %s",
                                 e2, (row.get("raw_log") or "")[:200])

        # 安全类分流（量小，逐条走既有安全解析器；session=None 避开逐条查设备表）
        security_count = 0
        try:
            from app.services.syslog_service import parse_syslog, normalize_event
            for row, is_sec in zip(rows, security_flags):
                if not is_sec:
                    continue
                sp = await parse_syslog(None, row["raw_log"], None)
                if row["device_id"] is not None:
                    sp["device_id"] = row["device_id"]
                # src_ip 为空时补上来源 IP，便于安全面板定位来源
                if not sp.get("src_ip"):
                    sp["src_ip"] = row["src_ip"]
                await normalize_event(db, sp, None)
                security_count += 1
        except Exception as e:
            # 安全分流失败不应影响主表留存
            logger.warning("安全类日志分流失败（device_logs 已留存）：%s", e)
            _stats["last_error"] = f"security routing: {type(e).__name__}: {e}"
            try:
                await db.rollback()
            except Exception:
                pass

        try:
            await db.commit()
        except Exception:
            await db.rollback()

    _stats["stored"] += stored
    _stats["store_errors"] += lost
    _stats["security_routed"] += security_count
    return stored


async def flush_pending() -> int:
    """把队列中已攒的日志落盘，返回落盘条数。

    单轮处理量设上限（``_MAX_ROWS_PER_CYCLE``），避免日志风暴时一轮占满事件循环、
    拖慢其它后台循环与 API 响应；剩余条目留到下一轮。
    """
    global _last_flush, _flush_running
    if _flush_running:
        return 0
    _flush_running = True
    total = 0
    try:
        while total < _MAX_ROWS_PER_CYCLE:
            batch: list[tuple[str, str, datetime, str]] = []
            while len(batch) < settings.SYSLOG_BATCH_SIZE:
                try:
                    batch.append(_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if not batch:
                break
            try:
                total += await _persist(batch)
            except Exception as e:
                _stats["store_errors"] += 1
                _stats["last_error"] = f"{type(e).__name__}: {e}"
                logger.error("设备日志入库失败（本批 %d 条已丢弃）：%s", len(batch), e)
    finally:
        _last_flush = monotonic()
        _flush_running = False
    return total


async def _flush_loop() -> None:
    """周期性落盘：队列达到批量阈值立即刷，否则最多等 ``SYSLOG_FLUSH_INTERVAL`` 秒。"""
    while True:
        await asyncio.sleep(0.25)
        if _queue.empty():
            continue
        if (
            _queue.qsize() >= settings.SYSLOG_BATCH_SIZE
            or (monotonic() - _last_flush) >= settings.SYSLOG_FLUSH_INTERVAL
        ):
            await flush_pending()


# ---------------------------------------------------------------------------
# 生命周期（挂到 loop_supervisor）
# ---------------------------------------------------------------------------

async def syslog_udp_loop() -> None:
    """受监督的 UDP 接收循环：建 socket → 周期落盘。

    绑定失败会向上抛（由监督器记录并 15 秒后重启），并把原因写入 ``bind_error``
    供前端展示 —— 避免"设备在发、平台静默收不到"的隐形故障。
    """
    global _last_flush
    loop = asyncio.get_running_loop()
    _stats["bind_error"] = None

    if not settings.SYSLOG_UDP_ENABLED:
        logger.info("syslog UDP 接收器已通过 SYSLOG_UDP_ENABLED=false 关闭")
        # 保持协程存活，避免监督器反复重启
        while True:
            await asyncio.sleep(3600)

    try:
        transport, _protocol = await loop.create_datagram_endpoint(
            SyslogUDPProtocol,
            local_addr=(settings.SYSLOG_UDP_HOST, settings.SYSLOG_UDP_PORT),
        )
    except Exception as e:
        _stats["bind_error"] = f"{type(e).__name__}: {e}"
        logger.error(
            "syslog UDP 接收器绑定 %s:%s 失败：%s。"
            "排查：① Linux 下端口 <1024 需 CAP_NET_BIND_SERVICE（install.sh 生成的 "
            "systemd unit 已含 AmbientCapabilities=CAP_NET_BIND_SERVICE）；"
            "② 端口可能被占用，可在 .env 改 SYSLOG_UDP_PORT（设备侧需同步指向新端口）；"
            "③ 防火墙需放行该 UDP 端口入站。",
            settings.SYSLOG_UDP_HOST, settings.SYSLOG_UDP_PORT, e,
        )
        raise

    _stats["bound_port"] = settings.SYSLOG_UDP_PORT
    _stats["started_at"] = _now().isoformat()
    _stats["bind_error"] = None
    _last_flush = monotonic()
    logger.info(
        "syslog UDP 接收器已启动：%s:%s（设备侧需配置 loghost 指向本机该地址）",
        settings.SYSLOG_UDP_HOST, settings.SYSLOG_UDP_PORT,
    )

    await _refresh_device_cache()
    try:
        await _flush_loop()
    finally:
        transport.close()
        _stats["bound_port"] = None
        logger.info("syslog UDP 接收器已停止")
