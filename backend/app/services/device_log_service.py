"""设备日志解析与归一化引擎（设备日志中心 P0）。

背景：平台原有的 ``syslog_service.py`` 是面向**安全设备**（防火墙/IPS 的
`%%01SEC/4/PACKET_FILTER` 类）设计的。拿 H3C 真机的**运维日志**（`%%10IFNET/3/PHY_UPDOWN`、
`SHELL/5/SHELL_LOGIN`）喂进去，7 个真实样本 7 个落在 ``unknown`` 兜底分支，连接口名都提取不到。
本模块按真机格式重写解析规则，与 ``syslog_service`` 并存、各管一路：

    UDP 入口 → 解析 → 按内容分流
                       ├─ 安全类（SEC/ATK/IPS/PACKET_FILTER…）→ security_events（原安全面板）
                       └─ 运维类（IFNET/SHELL/STP/OSPF…）    → device_logs（本模块）

真机报文形态（实测原文）：

    %Sep 14 16:23:21:068 2026 SW2 SHELL/5/SHELL_LOGIN: admin logged in from 192.168.124.108.
    <190>Sep 14 16:23:24 2026 SW2 %%10IFNET/3/PHY_UPDOWN: Physical state on the interface GigabitEthernet1/0/1 changed to down.

两个容易踩的坑（均已在本模块处理）：
  1. **级别取消息体内的 ``/N/``，不取 PRI。** H3C 的 PRI 由 info-center 全局配置决定，
     与单条消息的实际级别不一致——实测 PRI=190 解出 severity 6，而消息内是 3。
  2. **设备时钟可能是 UTC。** 实测 H3C 出厂 `display clock` 返回 UTC，而平台界面是 GMT+8，
     直接入库会有 8 小时偏差。故本模块把解析出的墙钟时间按
     ``SYSLOG_DEVICE_TZ_OFFSET_HOURS`` 换算为 UTC 存储，同时保留原始时间串备查；
     列表默认按平台接收时间（``received_at``，恒定可靠）排序。
"""
import logging
import re
from calendar import monthrange
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# syslog severity（RFC 5424 §6.2.1），与 H3C 消息体内 ``/N/`` 同序
SEVERITY_NAMES: dict[int, str] = {
    0: "emergency",
    1: "alert",
    2: "critical",
    3: "error",
    4: "warning",
    5: "notice",
    6: "informational",
    7: "debug",
}

# 运维分类
CATEGORIES = ("link", "auth", "config", "protocol", "system", "security", "other")

# 前端下拉用的中文名（后端返回，避免前端硬编码两份）
CATEGORY_LABELS: dict[str, str] = {
    "link": "接口链路",
    "auth": "登录认证",
    "config": "配置变更",
    "protocol": "路由协议",
    "system": "系统设备",
    "security": "安全事件",
    "other": "其他",
}

SEVERITY_LABELS: dict[int, str] = {
    0: "紧急", 1: "告警", 2: "严重", 3: "错误",
    4: "警告", 5: "通知", 6: "信息", 7: "调试",
}

# ---------------------------------------------------------------------------
# 正则
# ---------------------------------------------------------------------------

# syslog PRI 前缀：<190>
_PRI_RE = re.compile(r"^<(?P<pri>\d{1,3})>")

# H3C / 华为 info-center 信息块：%%10IFNET/3/PHY_UPDOWN:
#   %{0,2}     0~2 个百分号（转发报文带 %% ，设备本地缓冲区输出只有 % 或不带）
#   \d{0,4}    %% 后的版本/标识数字（如 %%10 / %%01），改为可选
#   模块名     字母开头
#   /级别/助记符
# 兼容华为的 LINK_STATE(l)[0]: 后缀
_INFO_BLOCK_RE = re.compile(
    r"%{0,2}\d{0,4}(?P<module>[A-Za-z][A-Za-z0-9_]*)/"
    r"(?P<sev>[0-7])/"
    r"(?P<mnemonic>[A-Za-z0-9_]+)"
    r"\s*(?:\([\w\-]*\))?\s*(?:\[\d+\])?\s*:"
)

# 时间戳：按优先级尝试
# 1) RFC5424 / ISO8601（自带年份，可带时区）： 2026-09-14T16:23:24.123Z / +08:00
_TS_ISO_RE = re.compile(
    r"(?P<year>\d{4})-(?P<mon>\d{2})-(?P<day>\d{2})[T ]"
    r"(?P<hh>\d{2}):(?P<mm>\d{2}):(?P<ss>\d{2})"
    r"(?:\.(?P<frac>\d{1,6}))?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?"
)
# 2) RFC3164 家族（H3C 本地缓冲区 / 转发报文）： Sep 14 16:23:21:068 2026 / Sep 14 16:23:24
#    毫秒与年份都做成可选——真机两种形态都存在，年份缺失时按"最接近当前"推断。
_TS_SYSLOG_RE = re.compile(
    r"(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+"
    r"(?P<hh>\d{2}):(?P<mm>\d{2}):(?P<ss>\d{2})"
    r"(?::(?P<ms>\d{1,3}))?"
    r"(?:\s+(?P<year>\d{4}))?"
)

_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# 接口名（长名优先，避免 GigabitEthernet 被 GE 抢先匹配；末尾必须跟数字）
# 同时覆盖 H3C Comware 与华为 VRP 的命名（Vlanif / Eth-Trunk / XGigabitEthernet / 25GE…）
_IFACE_RE = re.compile(
    r"\b(?:"
    r"Twenty-FiveGigE|HundredGigE|Ten-GigabitEthernet|M-GigabitEthernet|XGigabitEthernet|"
    r"FortyGigE|GigabitEthernet|Bridge-Aggregation|Route-Aggregation|"
    r"Vlan-interface|Vlanif|Eth-Trunk|LoopBack|Loopback|Tunnel|Ethernet|Serial|"
    r"100GE|40GE|25GE|10GE|MEth|XGE|BAGG|RAGG|Wlan-ESS|Vlan|GE"
    r")\s*\d+(?:/\d+)*(?:\.\d+)*",
    re.IGNORECASE,
)

# 用户名（登录/登出/命令行审计）
_USER_RES = (
    re.compile(r"(?P<user>[\w.\-@\\]+)\s+logged\s+(?:in|out)", re.IGNORECASE),
    re.compile(r"User=(?P<user>[^;,\s]+)"),
    re.compile(r"user\s+(?P<user>[\w.\-@\\]+)\s+(?:logged|login)", re.IGNORECASE),
)

# 源 IP（登录来源 / 命令行审计的 -IPAddr=）
_SRC_IP_RES = (
    re.compile(r"from\s+(?P<ip>\d{1,3}(?:\.\d{1,3}){3})", re.IGNORECASE),
    re.compile(r"IPAddr=(?P<ip>\d{1,3}(?:\.\d{1,3}){3})"),
    re.compile(r"source\s+(?P<ip>\d{1,3}(?:\.\d{1,3}){3})", re.IGNORECASE),
)

_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

# ---------------------------------------------------------------------------
# 分类规则
# ---------------------------------------------------------------------------

# 助记符优先（比模块更精确）：SHELL 模块同时含登录与命令审计两类
_MNEMONIC_CATEGORY: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"LOGINFAIL", re.I), "auth"),
    (re.compile(r"^(?:SHELL|SSHS?|TELNET|AAA|RDS)_?(?:LOGIN|LOGOUT)", re.I), "auth"),
    (re.compile(r"(?:^|_)CMD(?:_|$)", re.I), "config"),
    (re.compile(r"^(?:SHELL|SSHS?|TELNET)_(?:LOGIN|LOGOUT|LOGINFAIL)", re.I), "auth"),
    (re.compile(r"(?:^|_)(?:PHY|LINK|PORT|IF)_?(?:UP|DOWN|UPDOWN)", re.I), "link"),
    (re.compile(r"(?:UP|DOWN|UPDOWN)$", re.I), "link"),
)

# 模块 → 分类
_MODULE_CATEGORY: dict[str, str] = {}
for _cat, _mods in {
    "security": ("SEC", "ATK", "IPS", "AV", "URL", "DPI", "AFC", "BVS", "PKT"),
    "auth": ("SHELL", "SSHS", "SSH", "TELNET", "AAA", "RDS"),
    "config": ("CFG", "CFGMAN", "CFGM", "CONFIG", "OPS"),
    "link": ("IFNET", "LINK", "ETH", "MAC", "AGG", "LAGG", "VLAN"),
    "protocol": (
        "OSPF", "BGP", "VRRP", "STP", "MSTP", "RSTP", "LACP", "LLDP", "ISIS",
        "RIP", "MPLS", "BFD", "DHCP", "ARP", "NTP", "SNMP", "DNS", "PBR",
        "PIM", "IGMP", "RRPP", "SMARTLINK", "ERPS", "IRF", "MAD",
    ),
    "system": (
        "DEV", "BOARD", "POWER", "FAN", "TEMP", "SYS", "CLOCK", "VERSION",
        "SLOT", "MEM", "CPU", "FS", "STORAGE", "TRANSCEIVER", "DIAG", "LINE",
        "SRM", "HA", "QOS", "ACL",
    ),
}.items():
    for _m in _mods:
        _MODULE_CATEGORY.setdefault(_m, _cat)

# 正文关键词兜底（无信息块时使用）
_CONTENT_CATEGORY: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"login\s*(?:fail|incorrect)|authentication\s+fail|认证失败", re.I), "auth"),
    (re.compile(r"logged\s+(?:in|out)|login\s+success", re.I), "auth"),
    (re.compile(r"command\s+is\b|configur|configuration\s+(?:changed|modified)", re.I), "config"),
    (re.compile(r"physical\s+state|protocol\s+state|link\s+(?:up|down)|changed\s+to\s+(?:up|down)", re.I), "link"),
    (re.compile(r"\b(?:ospf|bgp|vrrp|stp|lacp|lldp|neighbor)\b", re.I), "protocol"),
    (re.compile(r"\b(?:attack|intrusion|flood|virus|malware|packet\s*filter)\b", re.I), "security"),
    (re.compile(r"\b(?:reboot|restart|temperature|fan|power|memory|cpu)\b", re.I), "system"),
)

# 判安全类走 security_events 的模块
_SECURITY_MODULES = {"SEC", "ATK", "IPS", "AV", "URL", "DPI", "AFC", "BVS"}


# ---------------------------------------------------------------------------
# 时间解析
# ---------------------------------------------------------------------------

def _infer_year(month: int, day: int, now: datetime) -> int:
    """RFC3164 无年份：取「与当前时间最接近」的年份。

    单纯用 ``now.year`` 会在跨年时出错——12 月 31 日 23:59 的日志在 1 月 1 日
    收到时会被判成一年前。这里比较当年/上一年/下一年三个候选，取时间距离最小的。
    """
    best_year = now.year
    best_delta = None
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            cand = now.replace(
                year=year, month=month, day=min(day, monthrange(year, month)[1]),
                hour=0, minute=0, second=0, microsecond=0,
            )
        except ValueError:
            continue
        delta = abs((cand - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds())
        if best_delta is None or delta < best_delta:
            best_delta, best_year = delta, year
    return best_year


def _apply_device_offset(naive: datetime, offset_hours: float) -> datetime:
    """把设备墙钟时间按设备时区偏移换算为 UTC。

    ``offset_hours`` 是设备时钟相对 UTC 的偏移（设备用北京时间则为 8）。
    实测 H3C 出厂 ``display clock`` 返回 UTC，故默认 0；设备已配时区时可调。
    """
    tz = timezone(timedelta(hours=offset_hours))
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)


def extract_timestamp(
    text: str, now: datetime, offset_hours: float
) -> tuple[datetime | None, str | None, tuple[int, int] | None]:
    """从报文中提取时间戳。

    Returns:
        (UTC 时间, 原始时间串, 该串在 text 中的 (start, end))；
        未识别到时返回 (None, None, None)。
    """
    m = _TS_ISO_RE.search(text)
    if m:
        try:
            naive = datetime(
                int(m.group("year")), int(m.group("mon")), int(m.group("day")),
                int(m.group("hh")), int(m.group("mm")), int(m.group("ss")),
                int((m.group("frac") or "0").ljust(6, "0")),
            )
        except ValueError:
            naive = None
        if naive is not None:
            tz_raw = m.group("tz")
            if tz_raw:
                # 报文自带时区：直接按它换算，忽略设备偏移猜测
                if tz_raw == "Z":
                    aware = naive.replace(tzinfo=timezone.utc)
                else:
                    sign = 1 if tz_raw[0] == "+" else -1
                    tz_s = tz_raw[1:].replace(":", "")
                    aware = naive.replace(tzinfo=timezone(
                        timedelta(hours=sign * int(tz_s[:2]), minutes=sign * int(tz_s[2:4]))
                    )).astimezone(timezone.utc)
            else:
                aware = _apply_device_offset(naive, offset_hours)
            return aware, m.group(0), (m.start(), m.end())

    m = _TS_SYSLOG_RE.search(text)
    if m:
        try:
            month = _MONTHS[m.group("mon")]
        except KeyError:
            return None, None, None
        day = int(m.group("day"))
        year_raw = m.group("year")
        year = int(year_raw) if year_raw else _infer_year(month, day, now)
        try:
            naive = datetime(
                year, month, min(day, monthrange(year, month)[1]),
                int(m.group("hh")), int(m.group("mm")), int(m.group("ss")),
                int((m.group("ms") or "0").ljust(3, "0")) * 1000,
            )
        except ValueError:
            return None, None, None
        return _apply_device_offset(naive, offset_hours), m.group(0), (m.start(), m.end())

    return None, None, None


# ---------------------------------------------------------------------------
# 字段提取
# ---------------------------------------------------------------------------

def _extract_first(patterns, text: str) -> str | None:
    for pat in patterns:
        m = pat.search(text)
        if m:
            return m.group(m.lastindex) if m.lastindex else m.group(0)
    return None


def _classify(module: str | None, mnemonic: str | None, content: str) -> str:
    """判定运维分类。助记符 > 模块 > 正文关键词。"""
    if mnemonic:
        for pat, cat in _MNEMONIC_CATEGORY:
            if pat.search(mnemonic):
                return cat
    if module:
        cat = _MODULE_CATEGORY.get(module.upper())
        if cat:
            return cat
    for pat, cat in _CONTENT_CATEGORY:
        if pat.search(content):
            return cat
    return "other"


# ---------------------------------------------------------------------------
# 主解析入口
# ---------------------------------------------------------------------------

def strip_nul(text: str | None) -> str | None:
    """去掉 NUL(0x00) 字符。

    **这不是洁癖，是必须的**：实测真机 H3C 报文尾部会带 0x00 填充，例如
    ``%%10LIPC/4/LIPC_STCP_CHECK: Data stays in the receive buffer ... 10523.\\x00``。
    而 PostgreSQL 的 text/varchar **不允许存 0x00**，一旦带进去就是
    ``DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes``。

    更致命的是批量入库是一条 INSERT 多行：**一条坏报文会让整批（最多 500 条）全部失败**。
    实测在测试服上因此丢了 12 个批次的真实设备日志——正是"静默丢包 = 审计链断裂"。
    """
    if not text or "\x00" not in text:
        return text
    return text.replace("\x00", "")


# parse_device_log 返回值里所有的文本字段（入库前统一过一遍 strip_nul）
_TEXT_FIELDS = ("hostname", "module", "mnemonic", "category", "interface",
                "username", "src_ip", "content", "device_time_raw", "raw_log")


def parse_device_log(
    raw_log: str,
    *,
    now: datetime | None = None,
    tz_offset_hours: float = 0.0,
) -> dict:
    """解析一条 syslog 报文为归一化字段字典。

    永不抛异常——无法识别的报文会退化为「正文即整条报文」，保证不丢数据
    （设备日志中心的第一职责是留存，宁可字段缺失也不能丢）。

    Args:
        raw_log: 原始 syslog 报文（单行）。
        now: 当前时间（注入便于测试）。
        tz_offset_hours: 设备时钟相对 UTC 的偏移小时数。
    """
    raw_log = strip_nul(raw_log)
    now = now or datetime.now(timezone.utc)
    text = (raw_log or "").strip()

    result: dict = {
        "hostname": None,
        "module": None,
        "mnemonic": None,
        "severity": None,
        "severity_name": None,
        "category": "other",
        "interface": None,
        "username": None,
        "src_ip": None,
        "content": text,
        "device_time": None,
        "device_time_raw": None,
        "raw_log": raw_log,
    }

    if not text:
        return result

    # 1) PRI（仅作无信息块时的级别兜底；不作为主用级别来源）
    pri_severity = None
    m = _PRI_RE.match(text)
    if m:
        pri = int(m.group("pri"))
        if 0 <= pri <= 191:
            pri_severity = pri % 8

    # 2) 时间戳（在整条报文上搜索，前缀形态多样）
    device_time, ts_raw, ts_span = extract_timestamp(text, now, tz_offset_hours)
    result["device_time"] = device_time
    result["device_time_raw"] = ts_raw

    # 3) 信息块（模块/级别/助记符）
    info = _INFO_BLOCK_RE.search(text)
    if info:
        result["module"] = info.group("module").upper()
        sev = int(info.group("sev"))
        result["severity"] = sev
        result["severity_name"] = SEVERITY_NAMES.get(sev)
        result["mnemonic"] = info.group("mnemonic").upper()

    # 4) 主机名：时间戳之后、信息块之前的那一段
    if ts_span:
        tail = text[ts_span[1]:]
        if info:
            rel = info.start() - ts_span[1]
            host_region = tail[:rel] if rel > 0 else tail
        else:
            host_region = tail
        # 去掉 %% / % 残留与前导空白，取第一个 token
        host_region = host_region.strip().lstrip("%").strip()
        if host_region:
            result["hostname"] = host_region.split()[0][:64]

    # 5) 正文：助记符冒号之后；否则信息块之后；再否则整条
    if info:
        result["content"] = text[info.end():].strip() or text
    elif ts_span:
        result["content"] = text[ts_span[1]:].strip() or text

    content = result["content"]

    # 6) 级别兜底
    if result["severity"] is None and pri_severity is not None:
        result["severity"] = pri_severity
        result["severity_name"] = SEVERITY_NAMES.get(pri_severity)

    # 7) 接口名 / 用户名 / 源 IP
    im = _IFACE_RE.search(content) or _IFACE_RE.search(text)
    if im:
        result["interface"] = im.group(0)

    user = _extract_first(_USER_RES, content) or _extract_first(_USER_RES, text)
    if user:
        result["username"] = user[:64]

    ip = _extract_first(_SRC_IP_RES, content) or _extract_first(_SRC_IP_RES, text)
    if not ip:
        # 仅认证/配置类才用"首个 IPv4"兜底，避免误抓无关地址
        guess_cat = _classify(result["module"], result["mnemonic"], content)
        if guess_cat in ("auth", "config"):
            gm = _IPV4_RE.search(content)
            ip = gm.group(0) if gm else None
    if ip and all(0 <= int(p) <= 255 for p in ip.split(".")):
        result["src_ip"] = ip

    # 8) 分类
    result["category"] = _classify(result["module"], result["mnemonic"], content)

    # 9) 兜底清洗：所有文本字段都不允许带 NUL（PostgreSQL text 存不了，整批会挂）
    for fld in _TEXT_FIELDS:
        if result.get(fld) is not None:
            result[fld] = strip_nul(result[fld])

    return result


def is_security_log(parsed: dict) -> bool:
    """该条日志是否应走安全事件表（供一个入口、两条落库路径分流）。"""
    module = (parsed.get("module") or "").upper()
    return parsed.get("category") == "security" or module in _SECURITY_MODULES


def severity_level(parsed: dict) -> str:
    """把 0-7 级别映射为告警严重度（供联动建告警用）。"""
    sev = parsed.get("severity")
    if sev is None:
        return "info"
    if sev <= 2:
        return "critical"
    if sev == 3:
        return "major"
    if sev == 4:
        return "warning"
    if sev == 5:
        return "minor"
    return "info"
