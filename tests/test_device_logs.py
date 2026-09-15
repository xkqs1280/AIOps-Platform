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
}


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
