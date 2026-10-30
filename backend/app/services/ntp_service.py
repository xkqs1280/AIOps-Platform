"""NTP 授时服务（P1）。

职责分两条**完全独立**的链路，不要混在一起看：

  - **上行取时**（平台当 NTP 客户端）：定时向上游时间源（默认阿里云
    ``ntp.aliyun.com``）请求时间，算出"平台时钟与云时钟的偏差"，落
    ``ntp_offset_records`` 供大屏画曲线、供时钟偏差告警取数。
    平台**不自动调整自己的时钟** —— 改宿主时钟是危险动作，且容器/非 root
    部署下往往根本没有权限。只度量、只展示，同步由管理员手动触发。

  - **下行授时**（平台当 NTP 服务器）：在本机监听 UDP 123，应答设备的 NTP 请求，
    让网络设备以平台为时间源。这是"平台做时间源"方案的落地形态。

两条链路由 ``ntp_settings.server_enabled`` / ``upstream_enabled`` 两个开关分别控制，
默认**都关闭**（按需开启）。

--------------------------------
关于应答报文里的字段值（**真机验证得出的配方，改动前请先想清楚**）
--------------------------------
NTP 应答不是随便填 48 字节就能被设备接受的。实测 H3C 设备会校验若干字段，
其中最容易踩的两个坑：

1. **reference timestamp 必须填真实当前时间**。填 0 会让设备判定"该服务器的时钟
   尚未同步"，直接丢弃应答 —— 设备侧表现为"配了 ntp-service unicast-server 但
   display ntp-service status 一直不同步"，而平台这边看起来一切正常（包发了、
   也回包了），排查方向极易被带偏。

2. **stratum 不能填 0/1**。0 是"未指定/不可用"，1 是"一级服务器（直连原子钟/GPS）"，
   平台显然不是一级源。填 2（二级，经上游同步）才是诚实的自我描述，也是设备
   愿意接受的值。

其余字段取值见下方常量区的逐条注释。
"""
import asyncio
import logging
import math
import struct
import time
from collections import deque
from datetime import datetime, timezone
from time import monotonic

from sqlalchemy import select

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 协议常量
# ---------------------------------------------------------------------------

# NTP 纪元是 1900-01-01，Unix 纪元是 1970-01-01，两者相差 2208988800 秒。
# 协议里的时间戳 = 自 1900 年起经过的秒数（32 位整数部分 + 32 位小数部分）。
NTP_EPOCH_DELTA = 2208988800

# 首字节 bit0-1=LI(闰秒指示)、bit2-4=VN(版本)、bit5-7=Mode。
# 应答：LI=0（无告警）、VN=4（NTPv4，兼容 v3 客户端）、Mode=4（服务器）
# → 二进制 00 100 100 = 0x24
NTP_RESPONSE_FLAGS = 0x24

# 请求的 Mode=3（客户端）；除 Mode 外的字段在请求中通常全 0，不必校验。
NTP_MODE_CLIENT = 3
NTP_MODE_SERVER = 4

# 我们对外自称的层级。见模块 docstring 第 2 条：填 2 是诚实且被接受的。
SERVER_STRATUM = 2

# 参考时钟标识。对 stratum 2 而言这 4 字节是上游服务器的地址（IPv4）或摘要，
# 只作标识用途、不参与同步计算，设备不校验。填平台自己的助记符便于在设备的
# 对端信息里认出"这是 AIOps 平台在给我授时"。
REFERENCE_ID = b"AIOP"

# 精度：2 的 -20 次方 ≈ 0.95 微秒。平台不是硬件时间源，不必虚报更高精度。
PRECISION_EXP = -20

# 到参考源的往返时延 / 相对参考源的离散度。
# 这两个值会进入设备端的误差估计（root distance），填太小（如 0）会让设备算出一个
# 过于乐观的置信区间，填太大则会拉大设备端的误差、甚至被判为不可用。取 ~4ms/10ms
# 是与上游（公网 NTP）实际往返量级相符、且被设备接受的经验值。
ROOT_DELAY_SEC = 0.004
ROOT_DISPERSION_SEC = 0.010

NTP_PACKET_SIZE = 48

# 上行取时的采样周期与超时
UPSTREAM_POLL_INTERVAL = 300.0        # 5 分钟采一次（一天 288 条，量很小）
UPSTREAM_TIMEOUT = 5.0                # 单次请求超时
UPSTREAM_MAX_SAMPLES = 5              # 单轮取多次取中位数，剔除网络抖动带来的毛刺

# 绑定失败自诊断用的端口
NTP_PORT = 123

# 持有端口/睡眠期间的开关复核间隔（秒）。
# 这是「关掉开关后多久真正释放端口」的上界，取小一点保证"随手关"的手感；
# 代价只是每 2 秒一次布尔判断，开销可忽略。
# **为什么用轮询而不是 asyncio.Event**：事件对象在"已开启"时是 set 的，
# 此时 `wait()` 立即返回，会把"持有端口"和"等待到下一采样周期"都变成
# 紧密空转（前者反复绑/解端口，后者对上游疯狂发包）。轮询的语义直白、
# 不依赖事件初值，是这类"长驻持有 + 随时可停"场景更稳的写法。
PORT_HOLD_POLL = 2.0

# 下行授时「近期请求」保留条数（定长环形缓冲，超出自动丢最旧的）。
#
# 为什么不能只留"最后一条"：单值只能回答"最近有人来过"，回答不了三个真正要命的问题 ——
#   ① 哪些设备来取过时（关心的那几台华为到底来没来）；
#   ② 每台设备的实际轮询间隔（相邻同源记录的时间差）；
#   ③ 某台设备压根不出现（设备侧没配 / ACL 不通），这才是反向诊断的入口。
# 取 20 条而不是更多：设备数 × 2~3 个采样点已经够看清间隔，再多只是刷屏。
RECENT_REQUESTS_MAX = 20

# 各来源的**累计台账**（本次启动以来）—— 既供算间隔，也供"哪几台来取过时"。
#
# 为什么不能只靠上面那个环形缓冲来回答"哪些设备来过"：缓冲是定长的，而设备侧的
# 轮询节奏能差出量级。实测就遇上过一批设备处于**启动突发**（1 秒一发），
# 20 条窗口只能覆盖约 20 秒 —— 一台 64 秒前刚来过的设备会被整个挤出列表，
# 在页面上表现为"从没来过"。而这恰恰是反向诊断（设备侧没配 / ACL 不通）最怕的误判：
# 把"来过但被窗口吃掉"读成"没来"，会让人直奔错误方向去查设备。
#
# 每条的结构（下划线开头的是内部字段，不外发）：
#   _ts       最近一次请求时刻（epoch 浮点）—— 只用于算间隔与淘汰最旧的
#   first_at  首次请求时刻（ISO，供页面显示）
#   last_at   最近一次请求时刻（ISO）
#   count     累计请求次数
#   answered  其中被成功应答的次数
#   interval  最近一次算出的间隔（秒）
_source_stats: dict[str, dict] = {}

# _source_stats 的条目上限。正常情况就等于纳管设备数（十几到几百条），但 UDP 无连接，
# 扫描/杂包也会往里塞 IP，故设上限。超限时丢**最久以前**的那条 ——
# 它大概率是已经不再来的源，丢掉对排查的损失最小。
MAX_TRACKED_SOURCES = 512


# ---------------------------------------------------------------------------
# 时间戳编解码（纯函数，便于测试）
# ---------------------------------------------------------------------------

def to_ntp_timestamp(unix_seconds: float) -> bytes:
    """Unix 时间（秒，可带小数）→ NTP 8 字节时间戳。"""
    ntp = unix_seconds + NTP_EPOCH_DELTA
    if ntp < 0:
        ntp = 0.0
    integer = int(ntp)
    fraction = int((ntp - integer) * (1 << 32))
    # 防御：fraction 理论上 < 2^32，但浮点误差可能让它等于 2^32
    if fraction >= (1 << 32):
        integer += 1
        fraction = 0
    return struct.pack("!II", integer & 0xFFFFFFFF, fraction & 0xFFFFFFFF)


def from_ntp_timestamp(raw: bytes) -> float:
    """NTP 8 字节时间戳 → Unix 时间（秒，带小数）。全零返回 0.0。"""
    if len(raw) < 8:
        return 0.0
    integer, fraction = struct.unpack("!II", raw[:8])
    if integer == 0 and fraction == 0:
        return 0.0
    return (integer + fraction / (1 << 32)) - NTP_EPOCH_DELTA


def from_ntp_short(raw: bytes) -> float:
    """NTP 4 字节短格式（仅整数秒部分）→ Unix 时间（秒）。"""
    if len(raw) < 4:
        return 0.0
    (integer,) = struct.unpack("!I", raw[:4])
    if integer == 0:
        return 0.0
    return float(integer) - NTP_EPOCH_DELTA


# ---------------------------------------------------------------------------
# 应答报文构造（核心；真机验证过的字段配方）
# ---------------------------------------------------------------------------

def build_response(request: bytes, now_unix: float | None = None) -> bytes:
    """根据请求报文构造合规的 NTP 服务器应答。

    字段布局（RFC 5905 §7.3，48 字节定长）：
        0  : LI/VN/Mode
        1  : Stratum
        2  : Poll
        3  : Precision (signed)
        4-7: Root Delay
        8-11: Root Dispersion
        12-15: Reference ID
        16-23: Reference Timestamp   ← **必须真实时间**，见模块 docstring
        24-31: Originate Timestamp   ← 回填客户端发来的 transmit（req[40:48]）
        32-39: Receive Timestamp
        40-47: Transmit Timestamp

    Args:
        request: 收到的请求报文（至少 48 字节；不足时按补零处理）。
        now_unix: 当前 Unix 时间，缺省取系统当前时间。**仅供测试注入**——
            生产必须用真实系统时间，否则回填的 reference/transmit 会失真，
            轻则设备误差显示离谱，重则直接丢弃应答。

    Returns:
        48 字节应答报文。
    """
    if now_unix is None:
        now_unix = time.time()

    # 请求的 client transmit timestamp（位于 40-47）要原样回填到 originate。
    # 这是客户端借以配对"我发的那一次请求"的凭据；不回填或填错，客户端会认为
    # 这是无关的应答而丢弃。
    originate = request[40:48] if len(request) >= 48 else b"\x00" * 8

    # Poll：把客户端请求里的 poll 值原样回填（仅作建议间隔，设备不强制校验）。
    poll = request[2] if len(request) >= 3 else 6

    reference_ts = to_ntp_timestamp(now_unix)
    receive_ts = to_ntp_timestamp(now_unix)
    transmit_ts = to_ntp_timestamp(now_unix)

    packet = bytearray(NTP_PACKET_SIZE)
    packet[0] = NTP_RESPONSE_FLAGS
    packet[1] = SERVER_STRATUM
    packet[2] = poll & 0xFF
    packet[3] = PRECISION_EXP & 0xFF
    # Root Delay / Dispersion 是 16.16 定点数
    struct.pack_into("!I", packet, 4, int(ROOT_DELAY_SEC * (1 << 16)) & 0xFFFFFFFF)
    struct.pack_into("!I", packet, 8, int(ROOT_DISPERSION_SEC * (1 << 16)) & 0xFFFFFFFF)
    packet[12:16] = REFERENCE_ID[:4].ljust(4, b"\x00")
    packet[16:24] = reference_ts
    packet[24:32] = originate
    packet[32:40] = receive_ts
    packet[40:48] = transmit_ts
    return bytes(packet)


def is_valid_request(data: bytes) -> bool:
    """粗略过滤：非 48 字节、或 Mode 不是客户端请求的，不必应答。

    不做严格校验 —— NTP 是 UDP 无连接协议，端口上可能收到各种扫描包/杂包，
    这里只做最低限度的"看起来是不是 NTP 请求"判断，避免对任意 UDP 载荷都回包
    （那会把平台变成反射放大攻击的放大器，也不能应答非 NTP 流量）。
    """
    if len(data) < NTP_PACKET_SIZE:
        return False
    mode = data[0] & 0x07
    return mode in (NTP_MODE_CLIENT, 3)


# ---------------------------------------------------------------------------
# 进程级状态
# ---------------------------------------------------------------------------

_stats: dict = {
    "requests": 0,        # 收到的 NTP 请求数（含被过滤掉的杂包）
    "responses": 0,       # 成功发出的应答数
    "last_client": None,  # 最近一次请求的来源 IP（页面上显示"最后授时对象"）
    "last_request_at": None,
    "last_error": None,
    "bound_port": None,
    "bind_error": None,
    "bind_occupier": None,
    "bind_hints": None,
    "started_at": None,
    # 最近一次成功采样到的偏差（秒）；None 表示还没采到
    "last_offset_seconds": None,
    "last_offset_at": None,
    "last_round_trip": None,
    "last_upstream": None,
    "upstream_error": None,
}

# 近期请求环形缓冲（**定长**，最新在右）。元素结构见 ``_record_recent``。
#
# 放在 ``_stats`` 之外是有意的：``reset_state()`` 会把 ``_stats`` 的值统一抹成
# 0/None，对 deque 做"抹值"没有意义（要的是清空），混在一起反而容易被漏掉。
_recent_requests: deque = deque(maxlen=RECENT_REQUESTS_MAX)

# 运行时开关（页面可控），与 .env 总闸取「与」关系。
# ⚠️ 初值必须是 **False**：设计要求「总开关默认关闭，需要时手动开启」。
# 若这里写成 settings.NTP_*_ENABLED（总闸默认 True），进程一启动就是开启态，
# 会在没有人确认的情况下占用 UDP 123 并对外发请求 —— 与设计意图相反。
# 真实值由 load_persisted_settings() 从数据库恢复。
_runtime_server_enabled: bool = False
_runtime_upstream_enabled: bool = False
_server_event: asyncio.Event | None = None
_upstream_event: asyncio.Event | None = None
_loaded_from_db = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def reset_state() -> None:
    """把 NTP 服务恢复到初始状态（测试用）。

    ⚠️ 运行时开关复位为 **False**（功能默认关闭），而不是 ``settings.NTP_*_ENABLED``。
    那两个是**部署级总闸**（默认 True = 允许页面开启），不是"页面开关的初始值"。
    早期版本在这里用总闸初始化运行时开关，导致功能变成"默认开启"，与
    「总开关默认关闭，需要时手动开启」的设计要求直接相反 —— 也正因如此，
    ``load_persisted_settings()`` 里没有设置行时必须**显式**清零，不能只依赖本函数。
    """
    global _runtime_server_enabled, _runtime_upstream_enabled
    global _server_event, _upstream_event, _loaded_from_db
    for k in _stats:
        _stats[k] = 0 if k in ("requests", "responses") else None
    _recent_requests.clear()
    _source_stats.clear()
    _runtime_server_enabled = False
    _runtime_upstream_enabled = False
    _server_event = None
    _upstream_event = None
    _loaded_from_db = False


# ---------------------------------------------------------------------------
# 运行时开关
# ---------------------------------------------------------------------------

def _event(which: str) -> asyncio.Event:
    """取事件对象（首次调用按当前开关状态设定 set/clear）。

    用途**仅限一处**：让关闭态下的循环挂起在 ``await ev.wait()``，由
    ``set_*_enabled`` 调 ``ev.set()`` 唤醒它立刻回到循环头重新判定开关。
    事件 set 状态恒等于对应开关，所以关闭态必然阻塞。

    ⚠️ **不要在"已开启"的状态下 `await ev.wait()`** —— 此时事件是 set 的，
    wait() 会立即返回，把长驻等待变成紧密空转（本项目踩过：端口反复绑/解、
    对上游疯狂发包）。持有期的等待一律用 ``PORT_HOLD_POLL`` 轮询 + 复核开关，
    见 ``ntp_server_loop`` / ``ntp_upstream_loop`` 的注释。
    """
    global _server_event, _upstream_event
    if which == "server":
        if _server_event is None:
            _server_event = asyncio.Event()
            if server_enabled():
                _server_event.set()
        return _server_event
    if _upstream_event is None:
        _upstream_event = asyncio.Event()
        if upstream_enabled():
            _upstream_event.set()
    return _upstream_event


def server_enabled() -> bool:
    """当前是否应答设备 NTP 请求（.env 总闸 **且** 页面开关）。"""
    return bool(settings.NTP_SERVER_ENABLED and _runtime_server_enabled)


def upstream_enabled() -> bool:
    """当前是否向上游取时。"""
    return bool(settings.NTP_UPSTREAM_ENABLED and _runtime_upstream_enabled)


def set_server_enabled(value: bool) -> bool:
    """切换下行授时开关，并唤醒/挂起应答循环（不重启进程）。"""
    global _runtime_server_enabled
    _runtime_server_enabled = bool(value)
    ev = _event("server")
    if server_enabled():
        ev.set()
    else:
        ev.clear()
        # 关闭后立刻清掉失败态：否则页面会一边显示「已关闭」、一边挂着上次的
        # 「绑定失败」，看不出到底关没关。
        _stats["bind_error"] = None
        _stats["bind_occupier"] = None
        _stats["bind_hints"] = None
    return server_enabled()


def set_upstream_enabled(value: bool) -> bool:
    """切换上行取时开关。"""
    global _runtime_upstream_enabled
    _runtime_upstream_enabled = bool(value)
    ev = _event("upstream")
    if upstream_enabled():
        ev.set()
    else:
        ev.clear()
        _stats["upstream_error"] = None
    return upstream_enabled()


async def load_persisted_settings() -> dict | None:
    """启动时从 ``ntp_settings`` 恢复开关；返回 None 表示无记录（沿用 .env 默认）。"""
    global _runtime_server_enabled, _runtime_upstream_enabled, _loaded_from_db
    from app.database import async_session
    from app.models.ntp import NtpSetting

    try:
        async with async_session() as db:
            row = (await db.execute(
                select(NtpSetting).order_by(NtpSetting.id).limit(1)
            )).scalar_one_or_none()
    except Exception as e:  # 读不到不该阻断启动，退回 .env 默认值
        logger.warning("读取 NTP 设置失败，沿用 .env 默认值：%s", e)
        return None

    _loaded_from_db = True
    if row is None:
        # 无记录 = 从未在页面上开过 → 保持关闭（默认关闭的设计要求）。
        # 这里显式清零而不是"什么都不做"：模块级初值虽已是 False，
        # 但显式写出来才能让"无记录即关闭"这条语义不依赖另一处的初值约定。
        _runtime_server_enabled = False
        _runtime_upstream_enabled = False
        _event("server").clear()
        _event("upstream").clear()
        logger.info("NTP 尚无持久化设置，两个开关均保持默认关闭")
        return None

    _runtime_server_enabled = bool(row.server_enabled)
    _runtime_upstream_enabled = bool(row.upstream_enabled)
    _event("server").set() if server_enabled() else _event("server").clear()
    _event("upstream").set() if upstream_enabled() else _event("upstream").clear()
    logger.info(
        "NTP 设置已从数据库恢复：授时=%s，上行取时=%s",
        "开启" if server_enabled() else "关闭",
        "开启" if upstream_enabled() else "关闭",
    )
    return {"server_enabled": server_enabled(), "upstream_enabled": upstream_enabled()}


async def get_settings_row(db) -> "object | None":
    """取设置行（无则返回 None）。供路由层复用，避免各处在写同一段 select。"""
    from app.models.ntp import NtpSetting

    return (await db.execute(
        select(NtpSetting).order_by(NtpSetting.id).limit(1)
    )).scalar_one_or_none()


def effective_upstream_server(row=None) -> str:
    """生效的上游时间源地址：设置行 → .env 默认值。"""
    if row is not None and getattr(row, "upstream_server", None):
        return str(row.upstream_server).strip()
    return settings.NTP_UPSTREAM_SERVER


def server_status() -> dict:
    """NTP 服务运行状态（供 API/前端展示）。"""
    from app.services.loop_supervisor import heartbeats

    beat = heartbeats().get("ntp-server")
    up_beat = heartbeats().get("ntp-upstream")
    return {
        "server_enabled": server_enabled(),
        "server_configured": settings.NTP_SERVER_ENABLED,
        "server_running": beat is not None and (monotonic() - beat) < 30,
        "bound": _stats["bound_port"] is not None,
        "listen_host": settings.NTP_LISTEN_HOST,
        "port": settings.NTP_PORT,
        "bound_port": _stats["bound_port"],
        "bind_error": _stats["bind_error"],
        "bind_occupier": _stats["bind_occupier"],
        "bind_hints": _stats["bind_hints"],
        "requests": _stats["requests"],
        "responses": _stats["responses"],
        "last_client": _stats["last_client"],
        "last_request_at": _stats["last_request_at"],
        # 近期请求列表（最新在前）。计数与列表都是**本次启动以来**的口径：
        # 两者同源同生命周期，前端标注一处即可，不必各自猜时间窗。
        "recent_requests": recent_requests(),
        "recent_requests_max": RECENT_REQUESTS_MAX,
        # 来源台账：窗口会被"勤快"的设备吃掉，所以"哪些设备来过"必须看这张表
        "recent_sources": recent_sources(),
        "recent_sources_max": MAX_TRACKED_SOURCES,
        "started_at": _stats["started_at"],
        "last_error": _stats["last_error"],

        "upstream_enabled": upstream_enabled(),
        "upstream_configured": settings.NTP_UPSTREAM_ENABLED,
        "upstream_running": up_beat is not None and (monotonic() - up_beat) < 60,
        "upstream_server": settings.NTP_UPSTREAM_SERVER,
        "last_offset_seconds": _stats["last_offset_seconds"],
        "last_offset_at": _stats["last_offset_at"],
        "last_round_trip": _stats["last_round_trip"],
        "last_upstream": _stats["last_upstream"],
        "upstream_error": _stats["upstream_error"],
        "threshold_seconds": settings.NTP_OFFSET_THRESHOLD_SECONDS,
    }


# ---------------------------------------------------------------------------
# 下行：UDP 123 应答
# ---------------------------------------------------------------------------

def _record_recent(client_ip: str | None, answered: bool,
                   reason: str | None = None) -> dict:
    """记一条「近期请求」（定长环形缓冲，超 ``RECENT_REQUESTS_MAX`` 丢最旧）。

    顺带算出 ``interval_seconds`` = **与上一次同源请求的时间差**。这项在写入时算
    而不是交给前端算：它是"NTP 轮询周期"这个领域概念的唯一真源，前端只负责显示。
    放在前端算的话，任何新的展示位（大屏、导出）都要再实现一遍，早晚算歪。

    间隔取自每来源的台账 ``_source_stats``（与展示缓冲解耦），所以哪怕上一条已经被
    挤出列表，算出来的间隔依然是设备侧真实的轮询周期 —— 见 ``_source_stats`` 处的说明。

    Args:
        client_ip: 来源 IP（可能为 None —— 真实网络栈下不会有，测试会）。
        answered: 是否成功发出了应答。未应答的也会入列并带 ``reason``：
            设备在发、平台不回是最需要立刻看见的故障，不能因为"没应答成功"就藏掉。
        reason: 未应答的原因（"非法请求" / 异常类型），成功时为 None。
    """
    now = _now()
    now_ts = now.timestamp()
    at_iso = now.isoformat()
    interval: float | None = None
    if client_ip:
        st = _source_stats.get(client_ip)
        if st is None:
            st = {
                "_ts": now_ts, "first_at": at_iso, "last_at": at_iso,
                "count": 0, "answered": 0, "interval": None,
            }
            _source_stats[client_ip] = st
        else:
            # max(0.0) 防御时钟回拨（NTP 场景下真会遇上）：负间隔会让页面显示成怪值
            interval = max(0.0, now_ts - st["_ts"])
        st["_ts"] = now_ts
        st["last_at"] = at_iso
        st["count"] += 1
        if answered:
            st["answered"] += 1
        st["interval"] = interval
        if len(_source_stats) > MAX_TRACKED_SOURCES:
            oldest = min(_source_stats, key=lambda ip: _source_stats[ip]["_ts"])
            _source_stats.pop(oldest, None)
    _recent_requests.append({
        "at": at_iso,
        "client_ip": client_ip,
        "answered": bool(answered),
        "reason": reason,
        # 首次出现/换周期前为 None，前端显示 "-"（不要显示 0，那会被读成"瞬间连发"）
        "interval_seconds": None if interval is None else round(interval, 3),
    })
    return _recent_requests[-1]


def recent_sources(limit: int | None = None) -> list[dict]:
    """本次启动以来出现过的来源台账（按最近一次请求倒序）。

    这是"哪几台真的来取过时"的**权威答案**：不受上面 20 条窗口的容量影响，
    所以"某台设备不在这张表里"才是可信的"它没来过"，可以直接去查设备侧配置与 ACL。
    """
    out = [
        {
            "client_ip": ip,
            "count": st["count"],
            "answered": st["answered"],
            "first_at": st["first_at"],
            "last_at": st["last_at"],
            "interval_seconds": (
                None if st["interval"] is None else round(st["interval"], 3)
            ),
        }
        for ip, st in _source_stats.items()
    ]
    # 按 ISO 字符串倒序即可（同一时区、定长格式 → 字典序 == 时间序）
    out.sort(key=lambda x: x["last_at"], reverse=True)
    if limit is not None and limit > 0:
        return out[:limit]
    return out


def recent_requests(limit: int | None = None) -> list[dict]:
    """近期请求列表（**最新在前**，便于页面直接顺序渲染）。"""
    items = list(_recent_requests)[::-1]
    if limit is not None and limit > 0:
        return items[:limit]
    return items


def handle_request(data: bytes, source_ip: str | None) -> bytes | None:
    """处理一个 NTP 请求（同步，供协议回调与测试调用）。

    Returns:
        应答报文；非法请求返回 None（不应答）。
    """
    _stats["requests"] += 1
    _stats["last_client"] = source_ip
    _stats["last_request_at"] = _now().isoformat()
    if not is_valid_request(data):
        _record_recent(source_ip, False, "非法请求（非 mode 3 客户端报文）")
        return None
    try:
        response = build_response(data)
    except Exception as e:  # 单包异常不允许影响应答循环
        _stats["last_error"] = f"{type(e).__name__}: {e}"
        logger.warning("构造 NTP 应答失败（来自 %s）：%s", source_ip, e)
        _record_recent(source_ip, False, f"构造应答失败：{type(e).__name__}")
        return None
    _stats["responses"] += 1
    _record_recent(source_ip, True)
    return response


class NtpUDPProtocol(asyncio.DatagramProtocol):
    """UDP NTP 服务器协议：收到请求即回包。

    处理必须**极快且不阻塞** —— UDP 无重传，处理慢一点客户端就超时重发，
    而重发又会带来更多负载。因此这里只做纯内存的报文构造与发送，不查库、不写日志
    （仅在异常时记 warning）。
    """

    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            response = handle_request(data, addr[0] if addr else None)
            if response and self.transport is not None:
                self.transport.sendto(response, addr)
        except Exception as e:  # pragma: no cover - 依赖真实网络栈
            _stats["last_error"] = f"{type(e).__name__}: {e}"
            logger.warning("处理 NTP 请求异常（来自 %s）：%s", addr, e)

    def error_received(self, exc) -> None:  # pragma: no cover - 依赖真实网络栈
        _stats["last_error"] = f"UDP error: {exc}"
        logger.warning("NTP UDP 错误：%s", exc)


# ---------------------------------------------------------------------------
# 绑定失败自诊断（复用 syslog_receiver 的成熟实现，避免两套逻辑漂移）
# ---------------------------------------------------------------------------

def _bind_hints(exc: BaseException, occupiers: list[str] | None = None) -> list[str]:
    """按平台生成可执行的排查建议。

    直接复用 syslog_receiver 的实现思路，但端口换成 123、配置项换成 NTP_*。
    不 import 复用是因为那边把端口硬编码进了文案，抽公共函数反而要动已稳定的模块。
    """
    import os
    import sys

    from app.services.syslog_receiver import udp_port_occupiers

    name = os.name
    winerr = getattr(exc, "winerror", None)
    errno_ = getattr(exc, "errno", None)
    occupiers = list(occupiers or [])
    port = settings.NTP_PORT
    hints: list[str] = []

    if name == "nt":
        if winerr == 10048 or errno_ == 10048:
            hints.append(
                "端口已被占用（WinError 10048）。找占用者："
                f"netstat -ano -p UDP | findstr :{port} 取最后一列 PID，"
                '再执行 tasklist /FI "PID eq <PID>" 看是哪个程序。'
            )
        elif winerr == 10013:
            hints.append(
                "访问被拒绝（WinError 10013）：端口可能落在系统保留区间，执行 "
                "netsh int ipv4 show excludedportrange protocol=udp 查看"
                "（Hyper-V / WSL / Docker 会预留端口段）。"
            )
        elif winerr == 10049:
            hints.append(
                "地址不可用（WinError 10049）：监听地址不是本机拥有的地址，"
                "请改回 0.0.0.0 或本机实际网卡地址。"
            )
        else:
            hints.append("Windows 下绑定 123 无需提权（Windows 没有 <1024 特权端口的概念）。")
        hints.append(
            "重点核查：① Windows 自带 W32Time 服务（Windows Time）常已占用 123，"
            "可执行 `net stop w32time` 停用（它是宿主自身的 NTP 客户端，与平台授时冲突）；"
            "② 是否重复启动了 AIOpsServer.exe；③ 是否装过其它 NTP 服务端软件。"
        )
    else:
        hints.append(
            "Linux 下绑定 <1024 端口需 CAP_NET_BIND_SERVICE（install.sh 生成的 systemd unit 已含 "
            "AmbientCapabilities=CAP_NET_BIND_SERVICE；若被手工覆盖或改用其它方式启动，该能力会丢失）。"
        )
        hints.append(
            f"找占用者：ss -lnup | grep ':{port}' 或 fuser -v {port}/udp。"
            "若宿主已装 chronyd/ntpd 且启用了 `allow`（对外授时）或 `port 123`，"
            "会与平台授时冲突 —— 二者只能留一个对外授时。"
        )

    if occupiers:
        own = os.path.basename(sys.executable).lower()
        joined = "、".join(occupiers)
        if own and any(own in o.lower() for o in occupiers):
            hints.insert(
                0,
                f"已查出占用者：{joined}，其中包含本平台自身进程（{own}）——通常是重复启动，"
                "结束多余实例后应答循环会在下一次重试（默认 15 秒）自动绑上。",
            )
        else:
            hints.insert(0, f"已查出占用者：{joined}，请确认该程序能否停用或改用其它端口。")
    return hints


# ---------------------------------------------------------------------------
# 下行：受监督的应答循环
# ---------------------------------------------------------------------------

async def ntp_server_loop() -> None:
    """受监督的 NTP 应答循环：按开关绑定/解绑 UDP 123。

    开关语义（页面上可切，不重启进程）：
      - 关闭 → 解绑端口（立刻释放 123，让宿主自身的 NTP 服务可重新占用），并挂起等待；
      - 开启 → 事件唤醒、重新绑定。

    绑定失败向上抛（由监督器记录并 15 秒后重启），把原因写入 ``bind_error``
    供前端展示 —— 避免出现"设备在请求、平台静默不回"的隐形故障。
    """
    ev = _event("server")
    while True:
        if not server_enabled():
            _stats["bound_port"] = None
            logger.info("NTP 授时应答服务处于关闭状态，等待开启")
            await ev.wait()
            continue

        loop = asyncio.get_running_loop()
        _stats["bind_error"] = None

        from app.services.syslog_receiver import udp_port_occupiers

        try:
            transport, _protocol = await loop.create_datagram_endpoint(
                NtpUDPProtocol,
                local_addr=(settings.NTP_LISTEN_HOST, settings.NTP_PORT),
            )
        except Exception as e:
            _stats["bind_error"] = f"{type(e).__name__}: {e}"
            occupiers = await asyncio.to_thread(udp_port_occupiers, settings.NTP_PORT)
            hints = _bind_hints(e, occupiers)
            _stats["bind_occupier"] = occupiers or None
            _stats["bind_hints"] = hints
            logger.error(
                "NTP 授时应答服务绑定 %s:%s 失败：%s。排查：%s",
                settings.NTP_LISTEN_HOST, settings.NTP_PORT, e,
                " ".join(f"({i}) {h}" for i, h in enumerate(hints, 1)),
            )
            raise

        _stats["bound_port"] = settings.NTP_PORT
        _stats["started_at"] = _now().isoformat()
        _stats["bind_error"] = None
        logger.info(
            "NTP 授时应答服务已启动：%s:%s（设备侧 ntp-service unicast-server 指向本机该地址）",
            settings.NTP_LISTEN_HOST, settings.NTP_PORT,
        )

        try:
            # 持有端口期间只需"等一个变化"，但**不能**直接 `await ev.wait()` ——
            # 事件此刻是 set 状态（正是"已开启"才走到这里），wait() 会立即返回，
            # 于是 finally 立刻关掉刚绑上的端口、循环回到顶部又重新绑定：
            # 表现为端口反复绑/解、服务状态里 bound 永远为 False、CPU 空转。
            # （Python 3.12 起有 wait() 的旧版实现就踩过这个坑，故显式写成
            #  "小睡 + 复核开关"的轮询，语义直白且不依赖事件初值。）
            while server_enabled():
                await asyncio.sleep(PORT_HOLD_POLL)
        finally:
            transport.close()
            _stats["bound_port"] = None
            logger.info("NTP 授时应答服务已停止（端口已释放）")


# ---------------------------------------------------------------------------
# 上行：取时 + 偏差测量
# ---------------------------------------------------------------------------

def compute_offset(req_time: float, resp_time: float,
                   server_recv: float, server_transmit: float) -> tuple[float, float]:
    """按 RFC 5905 计算客户端相对服务端的偏移与往返时延（纯函数）。

        offset = ((T2 - T1) + (T3 - T4)) / 2
        delay  = (T4 - T1) - (T3 - T2)

    其中 T1=客户端发送、T2=服务端接收、T3=服务端发送、T4=客户端收到。
    这样算出的 offset **抵消了大部分网络不对称时延**，是标准做法。

    ⚠️ **符号方向很容易搞反，这里说清楚**：
    该公式给出的是「**服务端时间 - 客户端时间**」的估计，即：
      - ``offset > 0`` → 服务端（上游）**快于**本机 → 本机时钟**慢**了，需要**往前调**；
      - ``offset < 0`` → 服务端（上游）**慢于**本机 → 本机时钟**快**了，需要**往回调**。

    直觉上容易记反（以为 offset 是"本机快多少"）。验证方法：设本机时钟快 1 秒，
    则 T1/T4 比真实时刻大 1，T2/T3 用服务端时间（真实时刻）→ (T2-T1) 为负 →
    offset 为负，与上面「本机快 → offset 负」一致。

    Returns:
        (offset_seconds, delay_seconds)
    """
    offset = ((server_recv - req_time) + (server_transmit - resp_time)) / 2.0
    delay = (resp_time - req_time) - (server_transmit - server_recv)
    return offset, delay


class _UpstreamProtocol(asyncio.DatagramProtocol):
    """一次性上游取时的 UDP 协议（收到一个应答即完成 future）。"""

    def __init__(self, future: asyncio.Future, sent_at: float) -> None:
        self.future = future
        self.sent_at = sent_at

    def datagram_received(self, data: bytes, addr) -> None:
        if self.future.done():
            return
        try:
            recv_time = time.time()
            if len(data) < NTP_PACKET_SIZE:
                return  # 杂包，继续等
            server_recv = from_ntp_timestamp(data[32:40])
            server_transmit = from_ntp_timestamp(data[40:48])
            if server_recv <= 0 or server_transmit <= 0:
                return
            self.future.set_result((self.sent_at, recv_time, server_recv, server_transmit))
        except Exception as e:  # pragma: no cover
            if not self.future.done():
                self.future.set_exception(e)

    def error_received(self, exc) -> None:  # pragma: no cover
        if not self.future.done():
            self.future.set_exception(exc)


async def query_upstream(server: str, timeout: float = UPSTREAM_TIMEOUT
                         ) -> tuple[float, float, float]:
    """向上游 NTP 服务器请求一次时间。

    Returns:
        (offset_seconds, round_trip_seconds, upstream_unix_time)

    Raises:
        asyncio.TimeoutError / OSError：解析失败、超时或网络不通。
    """
    loop = asyncio.get_running_loop()
    req = bytearray(NTP_PACKET_SIZE)
    req[0] = 0x1B  # LI=0, VN=4, Mode=3（客户端）
    sent_at = time.time()
    req[40:48] = to_ntp_timestamp(sent_at)

    fut: asyncio.Future = loop.create_future()
    transport, _proto = await loop.create_datagram_endpoint(
        lambda: _UpstreamProtocol(fut, sent_at),
        remote_addr=(server, NTP_PORT),
    )
    try:
        transport.sendto(bytes(req))
        result = await asyncio.wait_for(fut, timeout=timeout)
    finally:
        transport.close()

    t1, t4, t2, t3 = result
    offset, delay = compute_offset(t1, t4, t2, t3)
    return offset, delay, t3


async def sample_upstream(server: str, count: int = 1) -> tuple[float, float]:
    """连续取 ``count`` 次并取**中位数**，返回 (offset, delay)。

    为什么要多次取中位数：公网 NTP 的单次采样会被网络抖动污染（偶发一次 RTT 飙升
    会把 offset 算歪好几毫秒），中位数比均值更抗离群值。count=1 时退化为单次结果。
    """
    offsets: list[float] = []
    delays: list[float] = []
    last_exc: BaseException | None = None
    for _ in range(max(1, count)):
        try:
            off, dly, _t = await query_upstream(server)
            offsets.append(off)
            delays.append(dly)
        except Exception as e:  # 单次失败不算失败，继续尝试
            last_exc = e
            continue
    if not offsets:
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("上游取时失败：无任何有效样本")

    offsets.sort()
    delays.sort()
    mid = len(offsets) // 2
    offset = offsets[mid] if len(offsets) % 2 else (offsets[mid - 1] + offsets[mid]) / 2.0
    delay = delays[mid] if len(delays) % 2 else (delays[mid - 1] + delays[mid]) / 2.0
    return offset, delay


async def record_offset(offset: float, delay: float | None, server: str,
                        source: str = "upstream", error: str | None = None,
                        success: bool = True) -> None:
    """把一次偏差采样写入 ``ntp_offset_records``，并刷新内存态。

    落库失败**不向上抛** —— 采样循环不能因为一次数据库抖动就崩掉重启。
    """
    _stats["last_offset_seconds"] = offset if success else None
    _stats["last_offset_at"] = _now().isoformat()
    _stats["last_round_trip"] = delay
    _stats["last_upstream"] = server
    _stats["upstream_error"] = error

    from app.database import async_session
    from app.models.ntp import NtpOffsetRecord

    try:
        async with async_session() as db:
            db.add(NtpOffsetRecord(
                offset_seconds=float(offset),
                round_trip_seconds=float(delay) if delay is not None else None,
                source_address=server,
                source=source,
                success=success,
                error=error,
                sampled_at=_now(),
            ))
            await db.commit()
    except Exception as e:
        logger.warning("写入 NTP 偏差采样失败（不影响采样循环）：%s", e)


async def ntp_upstream_loop() -> None:
    """受监督的上行取时循环：按开关工作，周期采样并落库。

    采样周期固定 5 分钟。这里刻意**不接告警判定** —— 告警由
    ``alert_rule_engine`` 读 ``ntp_offset_records`` 统一评估，保持"一个告警引擎"
    的架构，避免服务层偷偷造告警导致告警收敛/抑制逻辑失效。
    """
    ev = _event("upstream")
    while True:
        if not upstream_enabled():
            logger.info("NTP 上行取时处于关闭状态，等待开启")
            await ev.wait()
            continue

        # 取设置行拿上游地址（允许页面上随时改源，无需重启进程）
        server = settings.NTP_UPSTREAM_SERVER
        try:
            from app.database import async_session
            from app.models.ntp import NtpSetting
            async with async_session() as db:
                row = (await db.execute(
                    select(NtpSetting).order_by(NtpSetting.id).limit(1)
                )).scalar_one_or_none()
            server = effective_upstream_server(row)
        except Exception as e:
            logger.warning("读取 NTP 上游地址失败，使用默认值 %s：%s", server, e)

        try:
            offset, delay = await sample_upstream(server, count=UPSTREAM_MAX_SAMPLES)
            await record_offset(offset, delay, server)
            logger.debug("NTP 采样：%s 偏差 %.6fs，RTT %.6fs", server, offset, delay)
        except Exception as e:
            # 采样失败不抛出：监督器重启只会让失败循环空转，日志也没有增量信息。
            # 记录错误态到内存 + 落一条 success=False 的样本，供页面与排障使用。
            _stats["upstream_error"] = f"{type(e).__name__}: {e}"
            logger.warning("NTP 上行取时失败（%s）：%s", server, e)
            try:
                await record_offset(0.0, None, server, success=False,
                                    error=f"{type(e).__name__}: {e}")
            except Exception:
                pass

        # 睡到下一个采样周期。**不能用 `wait_for(ev.wait(), 300)`**：开启态下
        # 事件是 set 的，wait() 立即返回 → 退化成"采样完立刻再采样"的紧密循环，
        # 会对上游疯狂发包（可能被上游限流/封禁）。改成按周期小睡、每轮复核开关，
        # 关开关后最多 PORT_HOLD_POLL 秒内退出，不影响"随手关"的体验。
        slept = 0.0
        while slept < UPSTREAM_POLL_INTERVAL and upstream_enabled():
            await asyncio.sleep(PORT_HOLD_POLL)
            slept += PORT_HOLD_POLL


# ---------------------------------------------------------------------------
# 手动同步（管理员触发）
# ---------------------------------------------------------------------------

# 校时失败必须"逐条留证据"。2026-09-24 真机踩坑：.108 上 chronyc **明明存在**，
# 但 `chronyc makestep` 返回 "501 Not authorised"（chronyd 只允许 root / chrony
# 用户经 Unix socket 控制它，走网络——哪怕 127.0.0.1——一律拒绝），而当时的代码把
# 所有失败都归成"未找到可用的时间同步工具，请安装 chrony"，让人去装一个已经装了
# 的软件。文案比故障本身更费时间，所以这里改成把每条策略的真实原因带回来。
# ⚠️ 处置建议必须**按平台分开**。2026-09-24 本地 Windows 实测暴露：Windows 上两条
# 策略全失败时，原先把 Linux 那套建议（systemd / CAP_SYS_TIME / chronyc）原样贴出来，
# 等于让人去配一台不存在的 systemd —— 和"让人去装已装好的 chrony"是同一类问题。
_STEP_FAIL_HINT_LINUX = (
    "注：平台不会自行提权，也不会自动安装第三方组件。按上面每条原因对应处置，常见的有："
    "①【推荐】给服务授予校时能力 —— systemd 单元设 "
    "AmbientCapabilities=CAP_SYS_TIME 后重启服务（新版安装脚本已默认包含），"
    "这样无需以 root 运行整个后端即可校时；"
    "② 由管理员在宿主执行 sudo chronyc makestep；"
    "③ 以 root 运行的部署可直接用 date -s。"
)

_STEP_FAIL_HINT_WINDOWS = (
    "注：平台不会自行提权。按上面每条原因对应处置，常见的有："
    "①【推荐】启动并配置 Windows Time —— 以管理员运行 "
    "net start w32time，再用 "
    "w32tm /config /manualpeerlist:\"<时间源>\" /syncfromflags:manual /update "
    "指定时间源，最后 w32tm /resync；"
    "② 给平台服务的运行账户加\"更改系统时间\"权利 —— 本地安全策略"
    "（secpol.msc）→ 本地策略 → 用户权利分配 → 更改系统时间，加入该账户，"
    "或直接把服务改为以 LocalSystem 运行；"
    "③ 以管理员身份在宿主手工执行 Set-Date。"
    "另外注意：w32tm 对的是 Windows Time **自己配置的源**，未必是本页设置的上游。"
)


def _step_fail_hint(is_windows: bool) -> str:
    """按平台返回可照做的失败处置建议（抽成函数便于两端各写断言）。"""
    return _STEP_FAIL_HINT_WINDOWS if is_windows else _STEP_FAIL_HINT_LINUX


# 常见失败串 → 一句人话结论。逐条原因原样保留的同时，把"卡在哪一类"提到行首，
# 省掉让人对着 PowerShell 的 ~~~~ 装饰符猜的时间。
_FAIL_CAUSES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("服务尚未启动", "0x80070426", "not been started"),
     "Windows Time 服务未启动"),
    (("客户端没有所需的特权", "Access is denied", "拒绝访问",
      "Operation not permitted", "EPERM"),
     "进程权限不足，无法修改系统时钟"),
    (("501 Not authorised", "Not authorised"),
     "chronyd 拒绝未授权控制（需 root/chrony 用户经 Unix socket）"),
    (("Permission denied",), "权限不足"),
    (("未安装（PATH 中找不到该命令）",), "命令不存在"),
    (("执行超时",), "执行超时"),
)


def _classify_fail(detail: str) -> str | None:
    """把原始报错归类成一句结论；认不出就返回 None（绝不硬套）。"""
    for keys, label in _FAIL_CAUSES:
        if any(k in detail for k in keys):
            return label
    return None


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def _run_step_cmd(cmd: list[str], timeout: int = 20) -> tuple[bool, str]:
    """执行一条外部校时命令，**把真实输出带回来**（绝不吞错误）。

    ``stdin=DEVNULL`` 是必要的：``chronyc -a`` 这类命令会等口令输入，
    没有 stdin 时会一直挂到超时，日志里还看不出到底卡在哪。
    """
    import os
    import subprocess

    if _which(cmd[0]) is None:
        return False, "未安装（PATH 中找不到该命令）"
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            errors="replace", stdin=subprocess.DEVNULL,
            encoding=("mbcs" if os.name == "nt" else None),
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                           if os.name == "nt" else 0),
        )
    except subprocess.TimeoutExpired:
        return False, f"执行超时（>{timeout}s）"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if proc.returncode == 0:
        # 成功也要留输出：`w32tm /resync` 会告诉我们它其实对到了**哪个源**
        # （Windows Time 自己的配置，未必是本页设置的上游），这比单说"成功"可信。
        out = " ".join(((proc.stdout or "") + (proc.stderr or "")).split())[:300]
        return True, (f"输出：{out}" if out else "执行成功")
    detail = " ".join(((proc.stdout or "") + (proc.stderr or "")).split())[:300]
    return False, f"退出码 {proc.returncode}" + (f"：{detail}" if detail else "")


def _step_clock_inprocess(target_epoch: float) -> tuple[bool, str]:
    """用 Python 内置 ``clock_settime`` 直接校正宿主时钟 —— **首选路径**。

    优点有三：不需要 chrony / ntpdate / sntp 等任何外部工具；不要求以 root
    运行整个后端（只要 ``CAP_SYS_TIME`` 一项能力，systemd 里
    ``AmbientCapabilities=CAP_SYS_TIME`` 即可）；不经 shell、不丢小数秒。

    实测（.108，Ubuntu 26.04）：以 uid 1000 + ambient CAP_SYS_TIME 跑
    ``time.clock_settime(CLOCK_REALTIME, t)`` 成功；无该能力则
    ``PermissionError: Operation not permitted``。
    """
    import time as _time

    setter = getattr(_time, "clock_settime", None)
    if setter is None:
        return False, "当前解释器/平台不提供 clock_settime（仅 Linux 可用）"
    try:
        setter(_time.CLOCK_REALTIME, float(target_epoch))
    except PermissionError as e:
        return False, f"无权限：{e.strerror or e}（进程缺少 CAP_SYS_TIME）"
    except OSError as e:
        return False, f"{type(e).__name__}: {e.strerror or e}"
    return True, "执行成功"


def set_system_time(offset_seconds: float) -> tuple[bool, str]:
    """把本机时钟调整 ``offset_seconds``（同步调用，调用方需自己 to_thread）。

    ⚠️ **这是本模块唯一会改动宿主状态的操作**，属高危：调错方向/调错量会让平台
    自身时间跳变，进而影响所有依赖时间戳的功能（告警时间、审计流水、日志时间轴）。
    因此设计上要求：仅管理员、二次确认、且**只允许小幅校正**（超过 5 分钟一律拒绝，
    这种量级说明是配置错误而非漂移，应先去排查上游与宿主 NTP 服务）。

    按**策略阶梯**依次尝试，每条策略都留下真实诊断（失败文案必须能指出"卡在哪一条、
    为什么"，绝不允许笼统的"未找到工具"）：

      Linux   : clock_settime（需 CAP_SYS_TIME）→ date -s（需 root）
                → chronyc makestep（需 root/chrony 用户 + 宿主有 chronyd）
      Windows : w32tm /resync（需管理员 + Windows Time 在跑）
                → PowerShell Set-Date（需管理员）

    Returns:
        (是否成功, 说明文案)
    """
    import os
    import platform

    try:
        offset_seconds = float(offset_seconds)
    except (TypeError, ValueError):
        return False, f"偏差值非法（{offset_seconds!r}），未做任何改动。"

    # NaN / inf 会让 `abs(x) > 上限` 恒为 False 而绕过后面的安全阀 ——
    # 写时钟之前必须先把非有限值拦掉。
    if not math.isfinite(offset_seconds):
        return False, f"偏差值非法（{offset_seconds}），未做任何改动。"

    if abs(offset_seconds) > settings.NTP_MANUAL_SYNC_MAX_SECONDS:
        return False, (
            f"偏差 {offset_seconds:.3f}s 超出允许手动校正的上限 "
            f"{settings.NTP_MANUAL_SYNC_MAX_SECONDS:.0f}s。如此大的偏差通常不是"
            "时钟漂移，而是上游地址配错、宿主时区错误或宿主自身 NTP 服务异常，"
            "请先排查根因，不要直接校正。"
        )

    # 目标时刻 = 当前时刻 + 偏差。offset 语义是「上游 − 平台」，
    # 所以 offset<0（平台偏快）时把时间往**前**拨 —— 方向由算式自动决定，
    # 不要在调用处再判一次方向（两处判定必然有一处会写反）。
    target_epoch = time.time() + offset_seconds

    # 策略阶梯：(名称, 前提条件, 执行体)。按顺序试，第一条成功即止。
    attempts: list
    if os.name == "nt":
        # Windows：先让系统时间服务自己去同步（若已配好源），再退到
        # "按我们实测的偏差直接改时间"（Windows Time 常未配置或已停用）。
        attempts = [
            ("w32tm /resync", "需管理员，且 Windows Time 服务在运行",
             lambda: _run_step_cmd(["w32tm", "/resync", "/force"], timeout=15)),
            ("PowerShell Set-Date", "需管理员",
             lambda: _run_step_cmd(
                 ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                  f"Set-Date -Date (Get-Date).AddSeconds({offset_seconds!r})"],
                 timeout=25)),
        ]
    elif platform.system() == "Linux":
        attempts = [
            # 首选：无需任何外部工具，只要一项能力，且精度最高。
            ("内置接口 clock_settime", "需 CAP_SYS_TIME 能力",
             lambda: _step_clock_inprocess(target_epoch)),
            ("date -s", "需以 root 运行",
             lambda: _run_step_cmd(["date", "-s", f"@{target_epoch:.6f}"], timeout=10)),
            # 最后退到宿主已有的时间守护进程。注意它会把时钟对到
            # **宿主自己配置的源**，未必是我们的上游 —— 与预期不同时如实说明。
            ("chronyc makestep", "需 root 或 chrony 用户；对的是宿主自己的时间源",
             lambda: _run_step_cmd(["chronyc", "makestep"], timeout=20)),
        ]
    else:
        return False, f"当前平台（{platform.system()}）不支持自动校正宿主时钟，请手工处理。"

    failures: list[str] = []
    for name, need, run in attempts:
        try:
            ok, detail = run()
        except Exception as e:  # 单条策略自身炸了，不能带崩整个流程
            ok, detail = False, f"{type(e).__name__}: {e}"
        if ok:
            # 成功时也把命令输出带上：w32tm /resync 的输出会指出实际同步到的源，
            # 比一句光秃秃的"成功"更可核查。
            tail = detail if detail and detail != "执行成功" else ""
            return True, (f"已校正宿主时钟 {offset_seconds:+.3f} 秒（{name}）。"
                          + (f"{tail}" if tail else ""))
        cause = _classify_fail(detail)
        failures.append(f"· {name}（{need}）→ "
                        + (f"【{cause}】" if cause else "") + detail)

    return False, (
        f"未能校正宿主时钟（偏差 {offset_seconds:+.3f} 秒）。"
        f"已按顺序尝试 {len(attempts)} 种方式，逐条原因：\n"
        + "\n".join(failures)
        + "\n" + _step_fail_hint(os.name == "nt")
    )


async def manual_sync(server: str | None = None) -> dict:
    """管理员手动同步：采样 → 判断是否可校正 → 执行 → 补记一条 manual 样本。

    返回结构化结果供路由层直接转 JSON。**不抛异常** —— 失败信息原样返回给页面，
    这比 500 更有用（操作人需要知道到底卡在哪一步）。
    """
    from app.database import async_session
    from app.models.ntp import NtpSetting

    try:
        async with async_session() as db:
            row = (await db.execute(
                select(NtpSetting).order_by(NtpSetting.id).limit(1)
            )).scalar_one_or_none()
    except Exception:
        row = None

    target = (server or "").strip() or effective_upstream_server(row)

    try:
        offset, delay = await sample_upstream(target, count=UPSTREAM_MAX_SAMPLES)
    except Exception as e:
        msg = f"取时失败，未能同步：{type(e).__name__}: {e}"
        await record_offset(0.0, None, target, source="manual", success=False, error=msg)
        return {"success": False, "message": msg, "offset_seconds": None,
                "round_trip_seconds": None, "server": target}

    ok, msg = await asyncio.to_thread(set_system_time, offset)

    # 校正后**复核一次**：改了时钟 ≠ 时钟就对齐了。chronyc 走的是宿主自己的源，
    # 与我们的上游未必同源；宿主的时间守护进程也可能立刻把时钟又拨回去。
    # 残差如实回给页面 —— 别让操作人以为"点了成功就一定准了"。
    residual: float | None = None
    if ok:
        await asyncio.sleep(0.4)
        try:
            residual, _rtt = await sample_upstream(target, count=2)
        except Exception as e:
            logger.warning("同步后复核采样失败：%s", e)
        else:
            msg = f"{msg}复核残差 {residual:+.3f} 秒。"

    await record_offset(offset, delay, target, source="manual",
                        success=ok, error=None if ok else msg)
    return {"success": ok, "message": msg, "offset_seconds": offset,
            "round_trip_seconds": delay, "residual_seconds": residual,
            "server": target}
