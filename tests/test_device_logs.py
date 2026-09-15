# -*- coding: utf-8 -*-
"""设备日志中心回归测试：解析引擎 / 接收器 / API / loghost 下发。

重点覆盖两类最容易回归的点：
  1. **真机报文解析**——平台原有 syslog 规则对 H3C 运维日志 100% 落 unknown 兜底，
     这些用例锁定"真机格式必须解析出来"的契约，防止规则被改回去；
  2. **安全边界**——关键字 LIKE 转义、loghost 参数防命令注入、级别取自消息内
     而非 PRIm、时区换算。
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.device_log import DeviceLog, DeviceLogHostConfig
from app.services import syslog_receiver
from app.services.device_log_service import (
    is_security_log,
    parse_device_log,
    severity_level,
)

NOW = datetime(2026, 9, 14, 9, 0, 0, tzinfo=timezone.utc)

# 真机实测原文 + 各厂商常见形态
REAL_SAMPLES = {
    "logbuffer_login": (
        "%Sep 14 16:23:21:068 2026 SW2 SHELL/5/SHELL_LOGIN: admin logged in from 192.168.124.108.",
        dict(module="SHELL", severity=5, mnemonic="SHELL_LOGIN", hostname="SW2",
             category="auth", username="admin", src_ip="192.168.124.108"),
    ),
    "phy_down": (
        "<190>Sep 14 16:23:24 2026 SW2 %%10IFNET/3/PHY_UPDOWN: Physical state on the "
        "interface GigabitEthernet1/0/1 changed to down.",
        dict(module="IFNET", severity=3, mnemonic="PHY_UPDOWN", hostname="SW2",
             category="link", interface="GigabitEthernet1/0/1"),
    ),
    "protocol_down": (
        "<189>Sep 14 16:23:25 2026 SW2 %%10IFNET/3/PHY_UPDOWN: Protocol state on the "
        "interface GigabitEthernet1/0/1 changed to down.",
        dict(module="IFNET", severity=3, category="link", interface="GigabitEthernet1/0/1"),
    ),
    "shell_cmd": (
        "<190>Sep 14 16:23:30 2026 SW2 %%10SHELL/6/SHELL_CMD: -Line=vty0-"
        "IPAddr=192.168.124.56-User=admin; Command is display version",
        dict(module="SHELL", severity=6, mnemonic="SHELL_CMD", category="config",
             username="admin", src_ip="192.168.124.56"),
    ),
    "loginfail": (
        "<188>Sep 14 16:23:31 2026 SW2 %%10SHELL/3/SHELL_LOGINFAIL: Failed to login as "
        "admin from 192.168.124.99.",
        dict(module="SHELL", severity=3, mnemonic="SHELL_LOGINFAIL", category="auth",
             src_ip="192.168.124.99"),
    ),
    "link_updown": (
        "<190>Sep 14 16:23:40 2026 SW2 %%10LINK/5/LINK_UPDOWN: Link status of "
        "GigabitEthernet1/0/2 changed to up.",
        dict(module="LINK", severity=5, category="link", interface="GigabitEthernet1/0/2"),
    ),
    "huawei_vlanif": (
        "<189>Sep 14 16:23:41 2026 HW-SW %%01IFNET/4/LINK_STATE(l)[0]:The line protocol IP "
        "on the interface Vlanif10 has entered the DOWN state.",
        dict(module="IFNET", severity=4, mnemonic="LINK_STATE", hostname="HW-SW",
             category="link", interface="Vlanif10"),
    ),
    "rfc5424_tz": (
        "<190>2026-09-14T16:23:50+08:00 SW3 IFNET/3/PHY_UPDOWN: Physical state on the "
        "interface Ten-GigabitEthernet1/0/5 changed to down",
        dict(module="IFNET", severity=3, hostname="SW3", category="link",
             interface="Ten-GigabitEthernet1/0/5"),
    ),
    # 以下四条是 .108 上华为 S5700 实机直发平台的原文（2026-09-15 抓取）。
    # 关键差异：华为把**年份放在日期之后、时间之前**（H3C 放在时间之后）。
    # 漏认这一形态时时间戳匹配不上，hostname/正文会一起丢——真机上表现为
    # 华为设备日志的 hostname 全为 NULL，几十条日志全成了"未纳管"。
    "huawei_vrp_shellcmd": (
        "<189>Sep 15 2026 21:12:23 HSW1 %%01SHELL/5/CMDRECORD(l)[125]:Record command "
        'information. (Task=VT0 , Ip=**, User=**, Command="undo debugging all")',
        dict(module="SHELL", severity=5, mnemonic="CMDRECORD", hostname="HSW1",
             category="auth"),
    ),
    "huawei_vrp_cfm_save": (
        "<188>Sep 15 2026 21:12:20 HSW1 %%01CFM/4/SAVE(l)[120]:The user chose Y when "
        "deciding whether to save the configuration to the device.",
        dict(module="CFM", severity=4, mnemonic="SAVE", hostname="HSW1", category="config"),
    ),
    "huawei_vrp_vtyuserlogin": (
        "<189>Sep 15 2026 21:12:05 HSW1 LINE/5/VTYUSERLOGIN:OID "
        "1.3.6.1.4.1.2011.5.25.207.2.2 A user login. (UserIndex=34, UserName=admin, "
        "UserIP=192.168.124.108, UserChannel=VTY0)",
        dict(module="LINE", severity=5, mnemonic="VTYUSERLOGIN", hostname="HSW1",
             category="system", username="admin", src_ip="192.168.124.108"),
    ),
    "huawei_vrp_hwcm_traplog": (
        "<189>Sep 15 2026 21:12:21 HSW2 %%01HWCM/5/TRAPLOG(l)[51]:OID "
        "1.3.6.1.4.1.2011.6.10.2.1 configure changed. (EventIndex=7, CommandSource=1, "
        "ConfigSource=2, ConfigDestination=4)",
        dict(module="HWCM", severity=5, mnemonic="TRAPLOG", hostname="HSW2",
             category="config", src_ip=None),
    ),
}


def test_oid_is_not_mistaken_for_source_ip():
    """SNMP OID 的前四段不是 IP。

    华为报文 "OID 1.3.6.1.4.1.2011.6.10.2.1 configure changed." 实测被抓成
    1.3.6.1 存进 src_ip（.108 上有 68 条），因为它属于 config 类、走了
    「首个 IPv4」兜底分支。
    """
    oid_only = ("<189>Sep 15 2026 21:12:21 HSW2 %%01HWCM/5/TRAPLOG(l)[51]:OID "
                "1.3.6.1.4.1.2011.6.10.2.1 configure changed.")
    assert parse_device_log(oid_only, now=NOW)["src_ip"] is None

    # 正文里的真地址仍要抓得到（不能因为加了前瞻就漏掉）
    real = ("<188>Sep 15 2026 21:00:00 HSW1 %%01SHELL/3/SHELL_LOGINFAIL: Failed to "
            "login as admin from 192.168.124.99.")
    assert parse_device_log(real, now=NOW)["src_ip"] == "192.168.124.99"


async def test_build_rows_records_datagram_source_ip(db):
    """入库的 src_ip 必须是**报文的来源**，而不是正文里提到的地址。

    否则 H3C 的 "logged in from 10.0.0.9" 会把设备自身 IP 顶掉，
    华为的 OID 更会被误抓（见上）。正文里的地址仍完整保留在 content 中。
    """
    from datetime import datetime, timezone

    from app.services.syslog_receiver import build_rows

    raw = ("<189>Sep 15 2026 21:12:21 HSW2 %%01HWCM/5/TRAPLOG(l)[51]:OID "
           "1.3.6.1.4.1.2011.6.10.2.1 configure changed.")
    rows, _flags = await build_rows(
        [("192.168.124.204", raw, datetime.now(timezone.utc), "udp")])
    assert rows[0]["src_ip"] == "192.168.124.204"
    assert rows[0]["hostname"] == "HSW2"
    assert rows[0]["module"] == "HWCM"


# ---------------------------------------------------------------------------
# 1. 解析引擎
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(REAL_SAMPLES))
def test_real_device_log_samples_are_parsed(name):
    """真机报文必须解析出模块/级别/助记符/分类——这是本功能存在的意义。

    平台原有 syslog 规则在这些样本上 7/7 落 unknown 兜底，本用例锁定新契约。
    """
    raw, expect = REAL_SAMPLES[name]
    parsed = parse_device_log(raw, now=NOW)
    for key, want in expect.items():
        assert parsed[key] == want, f"{name}: {key} 期望 {want!r}，实际 {parsed[key]!r}"
    assert parsed["severity_name"] is not None
    # 正文必须剥掉前缀（否则列表里满屏是时间戳）
    assert "SHELL_LOGIN:" not in (parsed["content"] or "")
    assert "PHY_UPDOWN:" not in (parsed["content"] or "")


def test_severity_comes_from_message_not_pri():
    """级别必须取消息体内的 /N/，不能取 PRI。

    实测 H3C 的 PRI=190 解出 severity 6，而消息内实际是 3 —— 用 PRI 会把
    "接口 DOWN（error）" 误报成 "informational"，告警联动就全错了。
    """
    raw = ("<190>Sep 14 16:23:24 2026 SW2 %%10IFNET/3/PHY_UPDOWN: Physical state on the "
           "interface GigabitEthernet1/0/1 changed to down.")
    parsed = parse_device_log(raw, now=NOW)
    assert parsed["severity"] == 3          # 消息内 /3/
    assert parsed["severity"] != 190 % 8    # 不是 PRI
    assert severity_level(parsed) == "major"


def test_pri_used_only_as_fallback():
    """无信息块时才用 PRI 兜底（否则一些厂商报文会完全没有级别）。"""
    parsed = parse_device_log("<13>Sep 14 16:23:24 2026 HOST app: something happened", now=NOW)
    assert parsed["severity"] == 13 % 8 == 5


def test_timezone_offset_applied():
    """设备时钟偏移换算：报文 16:23:21 按 UTC 解释时，入库就是 16:23:21Z。"""
    raw = "%Sep 14 16:23:21:068 2026 SW2 SHELL/5/SHELL_LOGIN: admin logged in from 1.1.1.1."
    utc_parsed = parse_device_log(raw, now=NOW, tz_offset_hours=0)
    cn_parsed = parse_device_log(raw, now=NOW, tz_offset_hours=8)
    assert utc_parsed["device_time"].strftime("%H:%M:%S") == "16:23:21"
    # 设备用北京时间（+8）时，同一墙钟换算到 UTC 应减 8 小时
    assert cn_parsed["device_time"].strftime("%H:%M:%S") == "08:23:21"
    # 原始时间串必须保留，便于事后核对时区
    assert utc_parsed["device_time_raw"]


def test_explicit_tz_in_message_wins():
    """报文自带时区时，按报文时区换算，不受设备偏移配置影响。"""
    raw = ("<190>2026-09-14T16:23:50+08:00 SW3 IFNET/3/PHY_UPDOWN: Physical state on the "
           "interface Ten-GigabitEthernet1/0/5 changed to down")
    for offset in (0, 8):
        parsed = parse_device_log(raw, now=NOW, tz_offset_hours=offset)
        assert parsed["device_time"].strftime("%H:%M") == "08:23"  # 16:23+08:00 → 08:23Z


def test_missing_year_inferred_near_now():
    """RFC3164 无年份时按"最接近当前"推断，避免跨年把日志判早一年。"""
    jan1 = datetime(2027, 1, 1, 2, 0, 0, tzinfo=timezone.utc)
    parsed = parse_device_log("Sep 14 16:23:24 HOST app: msg", now=jan1)
    # 1 月 1 日收到 9 月的日志 → 应是上一年，而不是 2027
    assert parsed["device_time"].year == 2026


def test_unknown_format_degrades_without_losing_data():
    """识别不了的报文也必须留存（日志中心第一职责是不丢数据）。"""
    raw = "completely unknown vendor blah blah"
    parsed = parse_device_log(raw, now=NOW)
    assert parsed["category"] == "other"
    assert parsed["content"] == raw
    assert parsed["raw_log"] == raw


def test_empty_and_multiline_safe():
    assert parse_device_log("", now=NOW)["content"] == ""
    assert parse_device_log("   \r\n ", now=NOW)["raw_log"] == "   \r\n "


def test_security_vs_operation_routing():
    """安全类分流到 security_events，运维类只进 device_logs。"""
    sec = parse_device_log(
        "<134>Sep 14 16:24:01 2026 FW1 %%01SEC/4/PACKET_FILTER: "
        "SrcIP=10.1.1.1 DstIP=192.168.1.1 action=block", now=NOW)
    assert sec["category"] == "security"
    assert is_security_log(sec) is True

    ops = parse_device_log(REAL_SAMPLES["phy_down"][0], now=NOW)
    assert is_security_log(ops) is False


def test_interface_extraction_variants():
    """接口名提取要覆盖 H3C 与华为的常见命名（否则列表里没有可读的接口）。"""
    cases = {
        "Physical state on the interface GigabitEthernet1/0/1 changed to down": "GigabitEthernet1/0/1",
        "Line protocol on the interface Vlanif10 is down": "Vlanif10",
        "interface Ten-GigabitEthernet1/0/5": "Ten-GigabitEthernet1/0/5",
        "interface Bridge-Aggregation2": "Bridge-Aggregation2",
        "interface Eth-Trunk1": "Eth-Trunk1",
        "interface 100GE1/0/1": "100GE1/0/1",
        "no interface mentioned at all": None,
    }
    for text, want in cases.items():
        parsed = parse_device_log(f"<190>Sep 14 16:23:24 2026 SW %%10IFNET/3/PHY_UPDOWN: {text}", now=NOW)
        assert parsed["interface"] == want, f"{text!r} → {parsed['interface']!r}，期望 {want!r}"


# ---------------------------------------------------------------------------
# 2. 接收器：入队 / 批量入库 / 限流 / 溢出
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_receiver():
    syslog_receiver.reset_state()
    yield
    syslog_receiver.reset_state()


async def test_datagram_enqueues_and_flushes(db, device_factory):
    """UDP 报文入队后批量落盘，并正确关联设备。"""
    dev = await device_factory(name="SW2", ip="192.168.124.66")

    queued = syslog_receiver.handle_datagram(
        REAL_SAMPLES["logbuffer_login"][0].encode(), "192.168.124.66")
    assert queued == 1
    stored = await syslog_receiver.flush_pending()
    assert stored == 1

    rows = (await db.execute(DeviceLog.__table__.select())).all()
    assert len(rows) == 1
    assert rows[0].device_id == dev.id
    assert rows[0].module == "SHELL"
    assert rows[0].category == "auth"
    assert rows[0].log_source == "udp"
    assert syslog_receiver.receiver_status()["stored"] == 1


async def test_multiline_datagram_split(db):
    """一个 UDP 报文含多行时逐行入库（部分转发组件会合并多条）。"""
    payload = (REAL_SAMPLES["logbuffer_login"][0] + "\n" +
               REAL_SAMPLES["phy_down"][0] + "\n\n").encode()
    assert syslog_receiver.handle_datagram(payload, "10.0.0.1") == 2
    assert await syslog_receiver.flush_pending() == 2


async def test_unknown_source_kept_with_null_device(db):
    """未纳管设备发来的日志必须保留（device_id 为空），这是"漏管设备"的发现线索。"""
    syslog_receiver.handle_datagram(REAL_SAMPLES["phy_down"][0].encode(), "10.9.9.9")
    await syslog_receiver.flush_pending()
    rows = (await db.execute(DeviceLog.__table__.select())).all()
    assert len(rows) == 1
    assert rows[0].device_id is None
    assert rows[0].src_ip == "10.9.9.9"


async def test_rate_limit_drops_and_counts(db, monkeypatch):
    """超限流阈值必须丢弃**并计数**——静默丢包等于审计链断裂。"""
    monkeypatch.setattr(settings, "SYSLOG_RATE_MAX_PER_WINDOW", 3)
    monkeypatch.setattr(settings, "SYSLOG_RATE_WINDOW_SECONDS", 60)
    raw = REAL_SAMPLES["phy_down"][0].encode()
    accepted = sum(syslog_receiver.handle_datagram(raw, "10.1.1.1") for _ in range(10))
    assert accepted == 3
    st = syslog_receiver.receiver_status()
    assert st["dropped_rate"] == 7
    assert st["received"] == 10


async def test_queue_overflow_drops_and_counts(db):
    """队列满时丢弃并计数，不能撑爆内存。"""
    # 临时把队列换成只有 2 个槽位，模拟"落盘跟不上接收"的日志风暴
    syslog_receiver._queue = syslog_receiver.asyncio.Queue(maxsize=2)
    raw = REAL_SAMPLES["phy_down"][0].encode()
    assert syslog_receiver.handle_datagram(raw, "10.2.2.2") == 1
    assert syslog_receiver.handle_datagram(raw, "10.2.2.2") == 1
    # 第三个报文无槽位可放：必须丢弃并计数，而不是无限增长
    assert syslog_receiver.handle_datagram(raw, "10.2.2.2") == 0
    st = syslog_receiver.receiver_status()
    assert st["dropped_overflow"] == 1
    assert st["received"] == 3      # 收到 3 条都计数（含被丢弃的）
    assert st["queue_size"] == 2


async def test_gbk_payload_decoded(db):
    """GBK 中文报文不能因解码异常丢日志。"""
    raw = "<190>Sep 14 16:23:24 2026 SW2 %%10SHELL/5/SHELL_LOGIN: 管理员登录成功".encode("gbk")
    assert syslog_receiver.handle_datagram(raw, "10.3.3.3") == 1
    await syslog_receiver.flush_pending()
    rows = (await db.execute(DeviceLog.__table__.select())).all()
    assert "管理员登录成功" in rows[0].content


async def test_security_log_routed_to_both_tables(db, device_factory):
    """安全类日志同时进 device_logs（审计留存）与 security_events（安全面板）。"""
    from app.models.p3_security import SecurityEvent
    await device_factory(name="FW1", ip="192.168.124.67")
    syslog_receiver.handle_datagram(
        "<134>Sep 14 16:24:01 2026 FW1 %%01SEC/4/PACKET_FILTER: "
        "SrcIP=10.1.1.1 DstIP=192.168.1.1 action=block".encode(),
        "192.168.124.67")
    await syslog_receiver.flush_pending()

    assert len((await db.execute(DeviceLog.__table__.select())).all()) == 1
    assert len((await db.execute(SecurityEvent.__table__.select())).all()) == 1
    assert syslog_receiver.receiver_status()["security_routed"] == 1


def test_oversized_message_truncated(monkeypatch):
    """超长报文截断，防畸形报文撑爆内存。"""
    monkeypatch.setattr(settings, "SYSLOG_MAX_MESSAGE_BYTES", 64)
    n = syslog_receiver.handle_datagram(b"A" * 5000, "10.4.4.4")
    assert n == 1
    _ip, text, _ts, _src = syslog_receiver._queue.get_nowait()
    assert len(text) <= 64


async def test_nul_bytes_from_real_device_are_stripped(db, device_factory):
    """真机报文尾部的 NUL(0x00) 必须在入库前剥掉。

    实测 H3C 会发这样的报文（注意结尾的 0x00）：
      <188>Sep 15 10:30:26 2026 SW3 %%10LIPC/4/LIPC_STCP_CHECK: ... 10523.\\x00
    PostgreSQL 的 text/varchar **不允许** 0x00，而批量入库是一条 INSERT 多行 ——
    结果是一条坏报文让整批（最多 500 条）全部 DataError 失败。
    测试服实测因此丢了 12 个批次的真实设备日志，这条用例锁死该回归。
    """
    await device_factory(name="SW3", ip="192.168.124.69")
    raw = (b"<188>Sep 15 10:30:26 2026 SW3 %%10LIPC/4/LIPC_STCP_CHECK: Data stays in the "
           b"receive buffer for an over long time. Owner=ttymgrd\x00")
    assert syslog_receiver.handle_datagram(raw, "192.168.124.69") == 1
    assert await syslog_receiver.flush_pending() == 1

    rows = (await db.execute(DeviceLog.__table__.select())).all()
    assert len(rows) == 1
    assert "\x00" not in rows[0].raw_log
    assert "\x00" not in rows[0].content
    assert rows[0].module == "LIPC"
    assert rows[0].severity == 4
    assert syslog_receiver.receiver_status()["store_errors"] == 0


def test_parse_strips_nul_from_all_text_fields():
    """解析层也兜一遍：任何入口（HTTP ingest / 未来新增通道）都不能把 NUL 带进库。"""
    parsed = parse_device_log("<188>Sep 15 10:30:26 2026 SW3 %%10LIPC/4/X: done.\x00")
    for field in ("content", "raw_log", "hostname", "mnemonic", "module", "device_time_raw"):
        assert "\x00" not in (parsed[field] or ""), f"{field} 仍含 NUL"


async def test_one_bad_row_does_not_kill_batch(db, device_factory, monkeypatch):
    """单条畸形记录不能让整批日志一起丢——审计表的底线是"宁可慢不可整批丢"。"""
    await device_factory(name="SW2", ip="192.168.124.66")
    real_build = syslog_receiver.build_rows

    async def poison(items):
        rows, _flags = await real_build(items)
        bad = dict(rows[0])
        # 放一个数据库驱动无法绑定的值 → 这一行必然插入失败
        # （不用 received_at=None：它带 server_default，SQLAlchemy 会直接省略该列）
        bad["content"] = {"not": "bindable"}
        return [rows[0], bad], [False, False]

    monkeypatch.setattr(syslog_receiver, "build_rows", poison)
    syslog_receiver.handle_datagram(REAL_SAMPLES["phy_down"][0].encode(), "192.168.124.66")

    assert await syslog_receiver.flush_pending() == 1        # 好行照样入库
    rows = (await db.execute(DeviceLog.__table__.select())).all()
    assert len(rows) == 1
    assert syslog_receiver.receiver_status()["store_errors"] == 1


# ---------------------------------------------------------------------------
# 3. 查询 API（转义 / 筛选 / 统计）
# ---------------------------------------------------------------------------

async def _list(db, **over):
    """按路由默认参数直接调用列表端点（FastAPI 的 Query 默认值需手工补齐）。"""
    from app.routers.device_logs import list_device_logs
    kwargs = dict(page=1, page_size=50, device_id=None, missing_device=False, module=None,
                  category=None, mnemonic=None, severity=None, max_severity=None,
                  interface=None, username=None, keyword=None, start=None, end=None,
                  db=db, _user={"sub": "admin", "role": "admin"})
    kwargs.update(over)
    return await list_device_logs(**kwargs)


async def _seed(db, device, specs):
    for raw in specs:
        syslog_receiver.handle_datagram(raw.encode(), device.ip)
    await syslog_receiver.flush_pending()


async def test_api_list_and_filters(db, device_factory):
    dev = await device_factory(name="SW2", ip="192.168.124.66")
    await _seed(db, dev, [
        REAL_SAMPLES["logbuffer_login"][0],
        REAL_SAMPLES["phy_down"][0],
        REAL_SAMPLES["shell_cmd"][0],
    ])

    res = await _list(db)
    assert res["total"] == 3 and len(res["items"]) == 3
    # 默认按接收时间倒序
    assert res["items"][0]["received_at"] >= res["items"][-1]["received_at"]
    # 设备名/IP 已联查出来（列表要能直接显示设备）
    assert res["items"][0]["device_name"] == "SW2"

    assert (await _list(db, category="link"))["total"] == 1
    assert (await _list(db, module="SHELL"))["total"] == 2
    assert (await _list(db, device_id=dev.id))["total"] == 3
    assert (await _list(db, device_id=dev.id + 999))["total"] == 0
    assert (await _list(db, severity="3,5"))["total"] == 2
    assert (await _list(db, max_severity=3))["total"] == 1
    assert (await _list(db, interface="GigabitEthernet1/0/1"))["total"] == 1
    assert (await _list(db, username="admin"))["total"] == 2


async def test_api_missing_device_filter(db, device_factory):
    dev = await device_factory(name="SW2", ip="192.168.124.66")
    await _seed(db, dev, [REAL_SAMPLES["phy_down"][0]])
    syslog_receiver.handle_datagram(REAL_SAMPLES["phy_down"][0].encode(), "10.7.7.7")
    await syslog_receiver.flush_pending()
    assert (await _list(db, missing_device=True))["total"] == 1
    assert (await _list(db))["total"] == 2


async def test_keyword_like_wildcards_are_escaped(db, device_factory):
    """关键字里的 % 与 _ 必须被转义——否则用户搜 "100%" 会把全表带出来。

    用例设计要和"不转义时的行为"能区分开，否则测试恒真、等于没测：
      - "0%" 不转义 → 模式 %0%% → 命中任何含 "0" 的日志（这里 2 条）；
      - "shell_login" 不转义 → "_" 匹配任意单字符 → 命中正文里的 "shellXlogin"。
    """
    dev = await device_factory(name="SW2", ip="192.168.124.66")
    await _seed(db, dev, [
        REAL_SAMPLES["phy_down"][0],                     # 正文含 GigabitEthernet1/0/1
        "<190>Sep 14 16:23:24 2026 SW2 %%10IFNET/4/PHY_UPDOWN: "
        "Interface GigabitEthernet1/0/1 utilization reached 100%",
        "<190>Sep 14 16:23:24 2026 SW2 %%10IFNET/4/PHY_UPDOWN: shellXlogin token accepted",
    ])
    assert (await _list(db, keyword="0%"))["total"] == 1
    assert (await _list(db, keyword="shell_login"))["total"] == 0
    assert (await _list(db, keyword="shellXlogin"))["total"] == 1
    # 反斜杠本身也要转义，否则用户搜 "\" 会构造出非法 LIKE 模式
    assert (await _list(db, keyword="\\"))["total"] == 0


async def test_keyword_matches_device_and_content(db, device_factory):
    dev = await device_factory(name="核心交换机", ip="192.168.124.66")
    await _seed(db, dev, [REAL_SAMPLES["phy_down"][0]])
    assert (await _list(db, keyword="核心"))["total"] == 1
    assert (await _list(db, keyword="192.168.124.66"))["total"] == 1
    assert (await _list(db, keyword="changed to down"))["total"] == 1
    assert (await _list(db, keyword="nonexistent-xyz"))["total"] == 0


async def test_api_stats_and_filters_endpoints(db, device_factory):
    from app.routers.device_logs import device_log_filters, device_log_stats
    dev = await device_factory(name="SW2", ip="192.168.124.66")
    await _seed(db, dev, [
        REAL_SAMPLES["logbuffer_login"][0],
        REAL_SAMPLES["phy_down"][0],
        REAL_SAMPLES["shell_cmd"][0],
    ])

    stats = await device_log_stats(hours=24, start=None, end=None, device_id=None,
                                   category=None, keyword=None, db=db,
                                   _user={"sub": "admin"})
    assert stats["total"] == 3
    assert {r["category"] for r in stats["by_category"]} == {"auth", "link", "config"}
    assert {r["module"] for r in stats["by_module"]} == {"SHELL", "IFNET"}
    assert stats["by_device"][0]["device_name"] == "SW2"
    assert sum(r["count"] for r in stats["by_hour"]) == 3
    assert sum(r["count"] for r in stats["by_severity"]) == 3

    flt = await device_log_filters(hours=24, db=db, _user={"sub": "admin"})
    assert {m["module"] for m in flt["modules"]} == {"SHELL", "IFNET"}
    assert [d["name"] for d in flt["devices"]] == ["SW2"]
    assert len(flt["severities"]) == 8 and len(flt["categories"]) == 7


async def test_receiver_status_endpoint_reports_bind_error(db):
    from app.routers.device_logs import receiver_status
    st = await receiver_status(_user={"sub": "admin"})
    assert st["enabled"] is True
    assert st["port"] == settings.SYSLOG_UDP_PORT
    assert "bind_error" in st and "dropped_overflow" in st


async def test_receiver_status_exposes_configured_retention_days(db):
    """页面副标题的留存天数必须由后端下发实际生效值，不能前端硬编码。

    背景：页面曾写死"等保 2.0 要求网络日志留存 ≥ 6 个月"，而实际留存 180 天
    （自然月 6 个月是 181~184 天）——文案承诺比实现大。改为后端下发后，
    客户在 .env 调大 DEVICE_LOGS_DAYS，页面自动跟着变。
    """
    from app.routers.device_logs import receiver_status
    st = await receiver_status(_user={"sub": "admin"})
    assert st["retention_days"] == settings.DEVICE_LOGS_DAYS


async def test_http_ingest_endpoint(db, device_factory):
    from app.routers.device_logs import IngestRequest, ingest_device_log
    await device_factory(name="SW2", ip="192.168.124.66")
    res = await ingest_device_log(
        IngestRequest(raw_log=REAL_SAMPLES["phy_down"][0], source_ip="192.168.124.66"),
        db=db, _user={"sub": "admin"},
    )
    assert res["queued"] == 1
    rows = (await db.execute(DeviceLog.__table__.select())).all()
    assert len(rows) == 1 and rows[0].log_source == "http"


def test_ingest_endpoint_requires_rate_limit():
    """接入端点必须挂 ``limit_ingest``（与 /syslog、/traps 保持一致）。

    队列上限（``QUEUE_MAX``）只能兜住「攒批层」，挡不住请求层突发；这条依赖
    一旦被摘掉，接入端点就成了无限流的写入口。用断言锁死，防回归。
    """
    from app.routers import device_logs as mod
    from app.services.rate_limit import limit_ingest

    route = next(
        r for r in mod.router.routes
        if getattr(r, "path", "").endswith("/ingest")
        and "POST" in getattr(r, "methods", set())
    )
    dep_calls = {d.call for d in route.dependant.dependencies}
    assert limit_ingest in dep_calls


# ---------------------------------------------------------------------------
# 4. loghost 下发：命令生成与防注入
# ---------------------------------------------------------------------------

def test_build_apply_and_rollback_commands():
    from app.services.device_loghost_service import (
        build_apply_commands, build_rollback_commands,
    )
    cmds = build_apply_commands("192.168.124.108", save=True)
    assert cmds[0] == "system-view"
    assert "info-center loghost 192.168.124.108" in cmds
    assert cmds[-1] == "save force"
    assert "return" in cmds

    # 非默认端口要带上 port
    cmds2 = build_apply_commands("192.168.124.108", port=5514, save=False)
    assert "info-center loghost 192.168.124.108 port 5514" in cmds2
    assert "save force" not in cmds2      # 不保存 = 只做运行时下发，便于灰度

    rb = build_rollback_commands("192.168.124.108")
    assert "undo info-center loghost 192.168.124.108" in rb
    # 下发前 info-center 是关闭状态时要一并还原
    rb2 = build_rollback_commands("192.168.124.108", restore_disabled=True)
    assert "undo info-center enable" in rb2


def test_huawei_uses_plain_save_instead_of_save_force():
    """华为 VRP 没有 `save force`：force 会被当成文件名而报错。

    实测 S5700-28C-HI(V200R001C00) / AR(VRP V500R011)：
        save force -> Error: Invalid file name or Invalid extension ( *.cfg, *.zip ).
    故华为必须用 `save`（交互确认由会话自动应答），H3C 继续用 `save force`。
    """
    from app.services.device_loghost_service import (
        build_apply_commands, build_rollback_commands,
    )
    hw = build_apply_commands("192.168.124.108", huawei=True)
    assert hw[-1] == "save"
    assert not any("save force" in c for c in hw)
    # 华为的 source 命令语法也不同：必须带 channel + log
    # （实测直接套用 H3C 写法会报 Unrecognized command 并把 ^ 指向 loghost）
    assert "info-center source default channel loghost log level informational" in hw
    assert "info-center source default loghost level informational" not in hw

    # 对照：H3C 仍是 save force + 短语法 source
    plain = build_apply_commands("192.168.124.108")
    assert plain[-1] == "save force"
    assert "info-center source default loghost level informational" in plain
    assert "channel loghost log level" not in plain

    # 两家共通的步骤完全一致，命令条数也一致（只差那两条的写法）
    for c in ("system-view", "info-center enable",
              "info-center loghost 192.168.124.108", "return"):
        assert c in hw and c in plain
    assert len(hw) == len(plain)

    hw_rb = build_rollback_commands("192.168.124.108", huawei=True)
    assert hw_rb[-1] == "save"
    assert not any("save force" in c for c in hw_rb)
    assert "undo info-center loghost 192.168.124.108" in hw_rb

    # 不保存（灰度下发）时两家都不带保存命令
    assert not any(c.startswith("save") for c in
                   build_apply_commands("192.168.124.108", save=False, huawei=True))


@pytest.mark.parametrize("vendor,model,expect", [
    ("华为", "S5700-28C-HI", True),
    ("Huawei", "S5700", True),
    ("huawei", None, True),
    (None, "AR (VRP V500R011)", True),      # 型号里带 VRP 同样按华为处理
    ("H3C", "S6850", False),
    ("H3C", "MSR36-20", False),
    ("", "", False),
    (None, None, False),
])
def test_is_huawei_vendor_detection(vendor, model, expect):
    """厂商判定要同时认 vendor 与 model：AR 设备的型号写作 "AR (VRP V500R011)"。"""
    from app.services.device_loghost_service import is_huawei

    class _D:
        pass

    d = _D()
    d.vendor, d.model = vendor, model
    assert is_huawei(d) is expect


def test_errors_in_ignores_huawei_save_confirm_noise():
    """华为 save 的确认交互会先打一行 'Error: Please choose ...'，但随即保存成功。

    实测该噪音与应答方式（Y / Y\\r\\n / Y\\n）无关，属固有行为。
    只在**同时**出现保存成功标志时才忽略，避免把真正的报错一起吞掉。
    """
    from app.services.device_loghost_service import DeviceSession
    s = DeviceSession.__new__(DeviceSession)   # errors_in 不依赖实例状态

    noisy_ok = (
        "save\n"
        "The current configuration will be written to the device.\n"
        "Are you sure to continue?[Y/N]\n"
        "Error: Please choose 'YES' or 'NO' first before pressing 'Enter'. [Y/N]:Y\n"
        "Now saving the current configuration to the slot 0.\n"
        "Save the configuration successfully.\n"
    )
    assert s.errors_in(noisy_ok) == []

    # 同样的噪音行但**没有**保存成功标志 -> 仍然报错（不掩盖真问题）
    noisy_bad = noisy_ok.replace("Save the configuration successfully.\n", "")
    assert s.errors_in(noisy_bad) == ["Error:"]

    # 华为上 save force 的真实报错不受影响
    real = "save force\nError: Invalid file name or Invalid extension ( *.cfg, *.zip ).\n"
    assert s.errors_in(real) == ["Error:"]

    # H3C 风格报错同样不受影响
    assert s.errors_in("% Unrecognized command found at '^' position.") == [
        "% Unrecognized command"]
    # 良性噪音与真错误同时出现时，只报真错误
    mixed = noisy_ok + "% Wrong parameter found at '^' position.\n"
    assert s.errors_in(mixed) == ["% Wrong parameter"]


async def test_read_until_idle_stops_at_success_marker():
    """保存命令要靠「成功标志」结束读取，而不是干等空闲阈值。

    根因：设备写盘期间会静默数秒，并发下发时按空闲阈值判定会提前收尾，
    漏掉 "Save the configuration successfully." -> errors_in 只剩确认交互的
    噪音行 -> 良性过滤失效 -> 明明保存成功却报 Error。
    """
    import asyncio
    import time

    from app.services.device_loghost_service import DeviceSession

    class _Reader:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        async def read(self, _n=65536):
            if self._chunks:
                return self._chunks.pop(0)
            await asyncio.sleep(30)        # 模拟设备写盘期间的静默
            return b""

    class _Writer:
        def write(self, _b):
            pass

        async def drain(self):
            pass

    reader = _Reader([
        b"save\nAre you sure to continue?[Y/N]\n",
        b"Now saving the current configuration to the slot 0.\n"
        b"Save the configuration successfully.\n",
    ])
    session = DeviceSession(_Writer(), reader)
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    out = await session._read_until_idle(
        8.0, loop.time() + 5, until=("Save the configuration successfully",))
    elapsed = time.monotonic() - started

    assert "successfully" in out
    assert session.errors_in(out) == []          # 噪音被识别为良性
    assert elapsed < 3, f"应见到成功标志即刻返回，实际等了 {elapsed:.1f}s"


@pytest.mark.parametrize("bad", [
    "192.168.1.1; reboot",              # 命令注入
    "192.168.1.1 && delete /unreserved",
    "not-an-ip",
    "192.168.1.1\nreboot",
    "",
    "192.168.1.1`id`",
])
def test_address_validation_blocks_injection(bad):
    """地址会被拼进设备命令行，必须严格校验——否则等于给了设备 root。"""
    from app.services.device_loghost_service import validate_address
    with pytest.raises(ValueError):
        validate_address(bad)


def test_level_and_port_validation():
    from app.services.device_loghost_service import validate_level, validate_port
    assert validate_level("WARNINGs") == "warnings"
    with pytest.raises(ValueError):
        validate_level("informational; reboot")
    assert validate_port(None) is None and validate_port(5514) == 5514
    for bad in (0, 65536, "abc"):
        with pytest.raises(ValueError):
            validate_port(bad)


def test_infocenter_disabled_detection():
    from app.services.device_loghost_service import infocenter_was_disabled
    assert infocenter_was_disabled("info-center enable\ninfo-center loghost 1.1.1.1") is False
    assert infocenter_was_disabled("undo info-center enable") is True
    assert infocenter_was_disabled(None) is False


async def test_apply_records_written_even_on_failure(db, device_factory, monkeypatch):
    """成功与失败都要落库留痕——失败记录是重试与追责的依据。"""
    from app.services import device_loghost_service as lh

    dev = await device_factory(name="SW2", ip="192.168.124.66")

    async def fake_ok(device, address, port=None, level="informational", save=True, timeout=0):
        return {"device_id": device.id, "device_name": device.name, "ip": device.ip,
                "ok": True, "before": "info-center enable", "after": f"info-center loghost {address}",
                "commands": "system-view", "warnings": [], "error": None}

    monkeypatch.setattr(lh, "apply_to_device", fake_ok)
    res = await lh.apply_loghost_to_devices(db, [dev], "192.168.124.108", operator="admin")
    assert res[0]["ok"] is True

    rows = (await db.execute(DeviceLogHostConfig.__table__.select())).all()
    assert len(rows) == 1
    assert rows[0].status == "applied"
    assert rows[0].loghost_address == "192.168.124.108"
    assert rows[0].applied_at is not None
    assert rows[0].operator == "admin"

    async def fake_fail(device, address, port=None, level="informational", save=True, timeout=0):
        return {"device_id": device.id, "device_name": device.name, "ip": device.ip,
                "ok": False, "before": None, "after": None, "commands": None,
                "warnings": [], "error": "登录超时"}

    monkeypatch.setattr(lh, "apply_to_device", fake_fail)
    await lh.apply_loghost_to_devices(db, [dev], "192.168.124.108")
    latest = [r for r in (await db.execute(
        DeviceLogHostConfig.__table__.select().order_by(DeviceLogHostConfig.id.desc())
    )).all()]
    assert latest[0].status == "failed"
    assert latest[0].message == "登录超时"
    assert latest[0].applied_at is None


async def test_apply_rejects_oversized_batch(db, device_factory, monkeypatch):
    from app.services import device_loghost_service as lh
    dev = await device_factory(name="SW2", ip="192.168.124.66")
    monkeypatch.setattr(lh, "MAX_DEVICES_PER_REQUEST", 1)
    with pytest.raises(ValueError):
        await lh.apply_loghost_to_devices(db, [dev, dev], "192.168.124.108")


def _ok_result(device):
    return {"device_id": device.id, "device_name": device.name, "ip": device.ip,
            "ok": True, "before": None, "after": None, "commands": None,
            "warnings": [], "error": None}


async def test_batch_worker_cancelled_error_does_not_blow_up_the_request(
    db, device_factory
):
    """单台设备的会话被取消，**不能**把整批请求变成 500。

    真实事故（70 台设备批量下发 → 前端只看到 "Request failed with status code 500"）：
    ``asyncio.CancelledError`` 自 Python 3.8 起继承 ``BaseException``，
    既躲过 ``apply_to_device`` 的 ``except Exception``，也躲过
    ``isinstance(item, Exception)`` 判定，最后在 ``idx, res = item`` 处抛
    TypeError；操作人看到"整批失败"，而设备其实已被改了一半。
    契约：每台设备恰好一条结果，被中断的那台按失败如实返回。
    """
    import asyncio
    from app.services import device_loghost_service as lh

    d1 = await device_factory(name="SW2", ip="192.168.124.66")
    d2 = await device_factory(name="SW3", ip="192.168.124.67")
    d3 = await device_factory(name="SW9", ip="192.168.124.69")

    async def worker(dev):
        if dev.id == d2.id:
            # 模拟会话读操作在内部被取消（连接中断），而**请求本身没被取消**
            raise asyncio.CancelledError("reader cancelled")
        return _ok_result(dev)

    res = await lh._run_bounded([d1, d2, d3], worker)

    assert len(res) == 3, "每台设备都必须有结果，不能被 gather 悄悄吞掉"
    assert [r["device_id"] for r in res] == [d1.id, d2.id, d3.id], "结果必须与入参同序"
    assert [r["ok"] for r in res] == [True, False, True]
    assert "CancelledError" in res[1]["error"]


async def test_batch_worker_base_exception_is_contained(db, device_factory):
    """连带 BaseException 的兜底：单台炸掉也不能连累整批（不能再出现 500）。"""
    from app.services import device_loghost_service as lh

    class _Weird(BaseException):
        pass

    d1 = await device_factory(name="SW2", ip="192.168.124.66")

    async def worker(dev):
        raise _Weird("底层库抛了 BaseException")

    res = await lh._run_bounded([d1], worker)
    assert len(res) == 1
    assert res[0]["ok"] is False
    assert "_Weird" in res[0]["error"]
    assert res[0]["device_id"] == d1.id


async def test_batch_request_cancellation_still_propagates(db, device_factory):
    """请求（任务）自己被取消时必须继续向上抛，不能被兜底吞掉。

    否则关停/断开时会把"取消"伪装成"设备失败"，既误导操作人，
    也让 uvicorn 的优雅关停失效。
    """
    import asyncio
    from app.services import device_loghost_service as lh

    d1 = await device_factory(name="SW2", ip="192.168.124.66")

    async def worker(dev):
        await asyncio.sleep(5)
        return _ok_result(dev)

    task = asyncio.create_task(lh._run_bounded([d1], worker))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_persist_failure_reports_truth_instead_of_500(
    db, device_factory, monkeypatch
):
    """留痕落库失败时如实返回逐台结果 + 警告，而不是回 500。

    设备配置这时**已经改过**了；回 500 会让操作人以为没生效而重复下发，
    对生产设备等于二次真实变更。契约：结果照返，warning 里说清"记录没落库"。
    """
    from app.services import device_loghost_service as lh

    dev = await device_factory(name="SW2", ip="192.168.124.66")

    async def fake_ok(device, address, port=None, level="informational", save=True, timeout=0):
        return _ok_result(device)

    async def boom():
        raise RuntimeError("relation \"device_loghost_configs\" does not exist")

    monkeypatch.setattr(lh, "apply_to_device", fake_ok)
    monkeypatch.setattr(db, "commit", boom, raising=False)

    res = await lh.apply_loghost_to_devices(db, [dev], "192.168.124.108")
    assert res[0]["ok"] is True, "设备侧结果不能被落库失败抹掉"
    assert any("落库失败" in w for w in res[0]["warnings"])
    assert res[0]["warnings"][-1].find("RuntimeError") >= 0


async def test_apply_requires_confirm(db, device_factory):
    """未确认时必须拒绝——真实改动设备配置不能靠误点触发。"""
    from fastapi import HTTPException
    from app.routers.device_logs import LoghostApplyRequest, loghost_apply
    dev = await device_factory(name="SW2", ip="192.168.124.66")
    with pytest.raises(HTTPException) as ei:
        await loghost_apply(
            LoghostApplyRequest(device_ids=[dev.id], address="192.168.124.108", confirm=False),
            request=None, db=db, user={"sub": "admin", "role": "admin"},
        )
    assert ei.value.status_code == 400


async def test_loghost_preview_returns_commands(db, device_factory):
    from app.routers.device_logs import LoghostTargetRequest, loghost_preview
    dev = await device_factory(name="SW2", ip="192.168.124.66", mgmt_protocol="ssh", mgmt_port=22)
    res = await loghost_preview(
        LoghostTargetRequest(device_ids=[dev.id], address="192.168.124.108"),
        db=db, _user={"sub": "admin"},
    )
    assert "info-center loghost 192.168.124.108" in res["commands"]
    assert res["devices"][0]["name"] == "SW2"
    assert res["rollback_commands"]

    # 华为与 H3C 混选时，预览必须逐台给出**实际**会执行的命令
    # （把 save force 显示给华为设备是误导操作人）
    hw = await device_factory(name="HSW1", ip="192.168.124.201", vendor="华为",
                              model="S5700-28C-HI", mgmt_protocol="ssh", mgmt_port=22)
    res2 = await loghost_preview(
        LoghostTargetRequest(device_ids=[dev.id, hw.id], address="192.168.124.108"),
        db=db, _user={"sub": "admin"},
    )
    by_name = {d["name"]: d for d in res2["devices"]}
    assert by_name["SW2"]["huawei"] is False
    assert by_name["SW2"]["commands"][-1] == "save force"
    assert by_name["HSW1"]["huawei"] is True
    assert by_name["HSW1"]["commands"][-1] == "save"
    assert not any("save force" in c for c in by_name["HSW1"]["commands"])
    assert {v["vendor"] for v in res2["variants"]} == {"H3C / 其他", "华为 VRP"}


# ---------------------------------------------------------------------------
# 5. 与既有功能的集成：删除设备不能销毁审计日志
# ---------------------------------------------------------------------------

async def test_device_delete_keeps_audit_logs(db, device_factory):
    """删除设备时日志**必须保留**（仅解除关联）。

    等保场景下日志是审计证据，因为设备从平台移除就删掉等于销毁证据；
    同时若不清 FK 会直接导致设备删除失败。
    """
    from sqlalchemy import select as sa_select
    from app.models.device import Device
    from app.routers.devices import _cleanup_device_relations

    dev = await device_factory(name="SW2", ip="192.168.124.66")
    dev_id = dev.id           # commit 会让 ORM 实例过期，后续访问 dev.id 会触发同步懒加载
    await _seed(db, dev, [REAL_SAMPLES["phy_down"][0], REAL_SAMPLES["logbuffer_login"][0]])
    db.add(DeviceLogHostConfig(device_id=dev_id, device_name="SW2", device_ip="192.168.124.66",
                               loghost_address="192.168.124.108", status="applied"))
    await db.commit()

    await _cleanup_device_relations(db, [dev_id])
    device = (await db.execute(sa_select(Device).where(Device.id == dev_id))).scalar_one_or_none()
    if device is not None:
        await db.delete(device)
    await db.commit()

    logs = (await db.execute(DeviceLog.__table__.select())).all()
    assert len(logs) == 2, "删除设备不应销毁设备日志"
    assert all(r.device_id is None for r in logs)
    cfgs = (await db.execute(DeviceLogHostConfig.__table__.select())).all()
    assert len(cfgs) == 1 and cfgs[0].device_id is None
    # 关联被解除，但快照必须还在——否则这条变更记录再也说不清改的是哪台设备
    assert cfgs[0].device_name == "SW2"
    assert cfgs[0].device_ip == "192.168.124.66"


# ---------------------------------------------------------------------------
# 6. 保留期
# ---------------------------------------------------------------------------

async def test_retention_deletes_only_expired_device_logs(db, device_factory):
    from app.services.retention_service import cleanup_expired_core_data
    dev = await device_factory(name="SW2", ip="192.168.124.66")
    now = datetime.now(timezone.utc)
    db.add_all([
        DeviceLog(device_id=dev.id, content="fresh", received_at=now - timedelta(days=1),
                  raw_log="x", category="link"),
        DeviceLog(device_id=dev.id, content="old", received_at=now - timedelta(days=200),
                  raw_log="x", category="link"),
    ])
    await db.commit()

    stats = await cleanup_expired_core_data()
    assert stats["device_logs"] == 1
    rows = (await db.execute(DeviceLog.__table__.select())).all()
    assert len(rows) == 1 and rows[0].content == "fresh"


async def test_retention_default_is_six_months():
    """等保要求日志留存 ≥ 6 个月，默认值不能被悄悄改小。"""
    from app.services import retention_service
    assert retention_service.DEVICE_LOGS_DAYS >= 180


def test_demo_data_endpoint_disabled_by_default():
    """演示数据接口默认关闭：造出来的数据混进审计数据在等保场景是硬伤。"""
    assert settings.DEMO_DATA_ENABLED is False


# ---------------------------------------------------------------------------
# 7. 配置回读与回验三态
#
# 现场事故（HX-7506X / 10.236.148.1）：命令已下发、设备上确实配好了，平台却报
# 「命令已下发但回验未在配置中找到目标」= 失败。根因是把「回读不到」当成了
# 「没配上」。下面这些用例把回读的健壮性口径与三态语义钉死。
# ---------------------------------------------------------------------------

def test_snapshot_lines_handles_bare_cr_line_endings():
    """设备只发 CR 作行分隔时，不能把整段粘成一行。

    原实现 `replace("\\r\\n", "\\n").split("\\n")` 遇到裸 CR 会让整段配置变成一行，
    行首不再是 `info-center loghost` → 回验必然失配；而同一文件里 `errors_in`
    用的是 `splitlines()`（认 CR），两处口径不一致，于是出现「报错检测正常、
    快照匹配全灭」这种最难查的组合。
    """
    from app.services.device_loghost_service import _has_loghost, _snapshot_lines

    raw = "<SW>display current-configuration | include info-center\r#\rinfo-center enable\rinfo-center loghost 10.236.148.62\r"
    text = _snapshot_lines(raw)
    assert "\r" not in text
    assert "info-center loghost 10.236.148.62" in text.splitlines()
    assert _has_loghost(text, "10.236.148.62") is True


def test_snapshot_lines_strips_ansi_and_noise():
    """控制序列 / 分页残留 / 错误指示符不能混进配置行。"""
    from app.services.device_loghost_service import _snapshot_lines

    raw = (
        "info-center enable\r\n"
        "\x1b[1;32minfo-center loghost 10.236.148.62\x1b[0m\r\n"
        "  ---- More ----\r\n"
        "        ^\r\n"
        "% Unrecognized command found at '^' position.\r\n"
        "<SW>display current-configuration | include info-center\r\n"
    )
    lines = _snapshot_lines(raw).splitlines()
    assert lines == ["info-center enable", "info-center loghost 10.236.148.62"]


def test_has_loghost_requires_full_address_on_a_loghost_line():
    """命中的必须是「日志主机配置行 + 完整 IP」，不能被正文里的地址糊弄。"""
    from app.services.device_loghost_service import _has_loghost

    hit = "info-center enable\ninfo-center loghost 10.236.148.62 port 514"
    assert _has_loghost(hit, "10.236.148.62") is True
    # 同一台设备上另一个日志主机 → 不算命中
    assert _has_loghost("info-center loghost 10.236.148.61", "10.236.148.62") is False
    # 仅出现在别的行（如描述/正文）里 → 不算命中
    assert _has_loghost("description send-to-10.236.148.62", "10.236.148.62") is False
    assert _has_loghost("", "10.236.148.62") is False
    # 华为 OID 前四段之类的前缀不能被当成地址命中（历史坑）
    assert _has_loghost("info-center loghost 1.3.6.1.4.1.2011", "10.236.148.62") is False


class _FakeReader:
    """按块吐出预定字节的假 reader（模拟 TCP 分片）。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, _n):
        return self._chunks.pop(0) if self._chunks else b""


class _FakeWriter:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)

    async def drain(self):
        return None


async def test_paging_marker_split_across_chunks_still_pages():
    """分页标记被 TCP 分片切开（`---- Mo` + `re ----`）也必须翻页。

    只看当前块的话会漏判 → 读到第一屏就因「空闲」结束 → 目标行在后面几页里
    永远看不到，同样是「配置明明生效却回验不到」的一种成因。
    """
    import asyncio
    from app.services.device_loghost_service import DeviceSession

    reader = _FakeReader([b"page1\r\n---- Mo", b"re ----", b"page2\r\n", b""])
    writer = _FakeWriter()
    session = DeviceSession(writer, reader)
    # 注意：deadline 是绝对时刻（run() 里传的是 loop.time() + timeout），不是时长
    loop = asyncio.get_running_loop()
    out = await session._read_until_idle(idle=0.05, deadline=loop.time() + 2.0)

    assert b" " in writer.written, "命中分页标记必须补一个空格翻页"
    assert "page2" in out


class _FakeSession:
    """按命令返回预设回包的假会话，用于验证回读退化链。"""

    def __init__(self, mapping, errors=None):
        self.mapping = mapping
        self.calls = []
        self._errors = errors or {}

    async def run(self, cmd, timeout=0, idle=0, until=None):
        self.calls.append(cmd)
        out = self.mapping.get(cmd, "")
        if isinstance(out, Exception):
            raise out
        return out

    def errors_in(self, out):
        return [m for m in self._errors.get(out, [])]


async def test_snapshot_falls_back_when_filter_command_errors():
    """`| include` 不被支持（明确报错）时，退化到更宽的回读命令。"""
    from app.services.device_loghost_service import SNAPSHOT_CMDS, fetch_infocenter_snapshot

    narrow = SNAPSHOT_CMDS[0]
    wider = SNAPSHOT_CMDS[1]
    sess = _FakeSession(
        {narrow: "% Unrecognized command found at '^' position.\n",
         wider: "info-center loghost 10.236.148.62\r\n"},
        errors={"% Unrecognized command found at '^' position.\n": ["% Unrecognized command"]},
    )
    snap = await fetch_infocenter_snapshot(sess)
    assert snap.trusted is True
    assert snap.command == wider
    assert "info-center loghost 10.236.148.62" in snap.text


async def test_snapshot_empty_readback_is_untrusted():
    """空回读一律不可信：无法区分「设备上真没有」与「读法不对」。"""
    from app.services.device_loghost_service import fetch_infocenter_snapshot

    snap = await fetch_infocenter_snapshot(_FakeSession({}))
    assert snap.trusted is False
    assert snap.text == ""
    assert snap.notes, "必须说明为什么不可信（哪条命令、什么现象）"


def test_judge_apply_three_states():
    """三态语义：确认生效 / 待确认 / 失败，三者不可互相冒充。"""
    from app.services.device_loghost_service import (
        STATE_APPLIED, STATE_FAILED, STATE_UNVERIFIED, Snapshot, _judge_apply,
    )

    addr = "10.236.148.62"
    # 1) 回读命中 → 已确认生效
    good = Snapshot(f"info-center loghost {addr}", "cmd", True)
    assert _judge_apply(addr, good, [])[0] == STATE_APPLIED

    # 2) 空回读 + 命令无报错 → 待确认（**不能**报失败，这才是 HX-7506X 的现场）
    empty = Snapshot("", "cmd", False, ["`cmd` 无输出"])
    state, msg = _judge_apply(addr, empty, [])
    assert state == STATE_UNVERIFIED
    assert "不可判定" in msg and addr in msg

    # 3) 回读可信但没有目标 → 确认失败
    other = Snapshot("info-center loghost 10.0.0.1", "cmd", True)
    assert _judge_apply(addr, other, [])[0] == STATE_FAILED

    # 4) 命令报错 → 确认失败（即便回读为空也不该说"待确认"）
    assert _judge_apply(addr, empty, ["% Unrecognized command"])[0] == STATE_FAILED


def test_judge_rollback_three_states():
    """回滚回验同样三态：空回读不能当成「已回滚成功」。"""
    from app.services.device_loghost_service import (
        STATE_FAILED, STATE_ROLLED_BACK, STATE_UNVERIFIED, Snapshot, _judge_rollback,
    )

    addr = "10.236.148.62"
    gone = Snapshot("info-center enable", "cmd", True)
    assert _judge_rollback(addr, gone, [])[0] == STATE_ROLLED_BACK
    still = Snapshot(f"info-center loghost {addr}", "cmd", True)
    assert _judge_rollback(addr, still, [])[0] == STATE_FAILED
    empty = Snapshot("", "cmd", False, ["`cmd` 无输出"])
    assert _judge_rollback(addr, empty, [])[0] == STATE_UNVERIFIED


async def test_unverified_apply_is_recorded_without_applied_at(db, device_factory, monkeypatch):
    """「待确认」要落库成独立状态，且不写 applied_at（否则状态表自相矛盾）。"""
    from app.services import device_loghost_service as lh

    dev = await device_factory(name="HX-7506X", ip="10.236.148.1")

    async def fake_apply(device, address, port=None, level="informational", save=True, timeout=0):
        return {"device_id": device.id, "device_name": device.name, "ip": device.ip,
                "state": "unverified", "ok": False, "before": None, "after": "",
                "commands": "system-view", "warnings": [],
                "error": "命令已下发且无报错，但回验不可判定（`cmd` 无输出）"}

    monkeypatch.setattr(lh, "apply_to_device", fake_apply)
    res = await lh.apply_loghost_to_devices(db, [dev], "10.236.148.62", operator="admin")
    assert res[0]["state"] == "unverified"

    row = (await db.execute(DeviceLogHostConfig.__table__.select())).one()
    assert row.status == "unverified"
    assert row.applied_at is None, "待确认不是已生效，不能盖 applied_at 时间戳"
    assert "不可判定" in row.message


async def test_unverified_device_still_can_be_rolled_back(db, device_factory):
    """「待确认」的设备必须能按记录回滚——它很可能已经把 loghost 配上了。"""
    from app.services import device_loghost_service as lh

    dev = await device_factory(name="HX-7506X", ip="10.236.148.1")
    db.add(DeviceLogHostConfig(
        device_id=dev.id, device_name=dev.name, device_ip=dev.ip,
        loghost_address="10.236.148.62", status="unverified",
        before_config="info-center enable",
    ))
    await db.commit()

    recs = await lh.latest_records(db, [dev.id])
    assert recs[dev.id].status == "unverified"
    assert lh.infocenter_was_disabled(recs[dev.id].before_config) is False
