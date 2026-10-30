"""NTP 授时功能的单元测试。

覆盖三个最容易出错的纯逻辑层（协议编解码 / 命令构建 / 回验判定），
这些都是"错了也不会报错、但生产上会静默失效"的地方 —— 正因如此必须有测试：

  - 报文配方错一个字段位，设备会静默丢弃应答（平台侧看起来一切正常）；
  - 命令构建错一条，下发会报 Unrecognized 并被回验判成失败；
  - 回验判定把 unverified 误判成 failed，会诱导操作人重复下发生产设备。
"""
import struct
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services import ntp_config_service as cfg
from app.services import ntp_service as ntp


# ---------------------------------------------------------------------------
# 时间戳编解码
# ---------------------------------------------------------------------------

def test_ntp_timestamp_roundtrip():
    """NTP 时间戳编解码应可往返（精度到 1/2^32 秒，约 0.23ns）。"""
    # 2025-09-21 12:00:00 UTC
    unix = 1758456000.0
    raw = ntp.to_ntp_timestamp(unix)
    assert len(raw) == 8
    back = ntp.from_ntp_timestamp(raw)
    assert abs(back - unix) < 1e-6


def test_ntp_timestamp_epoch_delta():
    """Unix 纪元 0 秒应对应 NTP 的 2208988800 秒。"""
    raw = ntp.to_ntp_timestamp(0.0)
    integer, _frac = struct.unpack("!II", raw)
    assert integer == ntp.NTP_EPOCH_DELTA == 2208988800


def test_from_ntp_timestamp_zero_returns_zero():
    """全零时间戳（客户端请求里未填的字段）应返回 0，而不是 -2208988800。"""
    assert ntp.from_ntp_timestamp(b"\x00" * 8) == 0.0


def test_ntp_timestamp_fraction_overflow_carries():
    """小数部分接近 1 时的进位不能溢出（否则 packed 值会回绕成负数）。"""
    raw = ntp.to_ntp_timestamp(1758456000.9999999999)
    integer, fraction = struct.unpack("!II", raw)
    assert 0 <= fraction < (1 << 32)
    assert integer >= ntp.NTP_EPOCH_DELTA


# ---------------------------------------------------------------------------
# 应答报文构造（真机验证过的配方，改动必须同步改这里）
# ---------------------------------------------------------------------------

def _client_request(sent_unix: float = 1758456000.0) -> bytes:
    req = bytearray(48)
    req[0] = 0x1B  # LI=0 VN=4 Mode=3（客户端）
    req[40:48] = ntp.to_ntp_timestamp(sent_unix)
    return bytes(req)


def test_build_response_is_48_bytes_with_compliant_header():
    resp = ntp.build_response(_client_request(), now_unix=1758456100.0)
    assert len(resp) == 48
    assert resp[0] == 0x24, "首字节必须是 0x24（LI=0 / VN=4 / Mode=4）"
    assert (resp[0] & 0x07) == ntp.NTP_MODE_SERVER
    assert ((resp[0] >> 3) & 0x07) == 4, "VN 必须是 4"


def test_build_response_stratum_is_two():
    """stratum 必须是 2：填 0/1 会让设备判定该服务器不可用。"""
    resp = ntp.build_response(_client_request())
    assert resp[1] == 2


def test_build_response_echoes_client_transmit_as_originate():
    """originate 必须回填客户端的 transmit 时间戳，否则客户端无法配对这次请求。"""
    req = _client_request(1758456000.25)
    resp = ntp.build_response(req)
    assert resp[24:32] == req[40:48]


def test_build_response_reference_time_is_real_not_zero():
    """**最关键的一条**：reference timestamp 必须填真实时间。

    填全零会让设备判定"该服务器时钟未同步"并直接丢弃应答 ——
    设备侧表现为配了服务器却一直不同步，而平台侧看起来毫无异常。
    """
    resp = ntp.build_response(_client_request(), now_unix=1758456100.0)
    reference = ntp.from_ntp_timestamp(resp[16:24])
    assert reference > 0, "reference timestamp 不能为 0"
    assert abs(reference - 1758456100.0) < 1e-6


def test_build_response_root_delay_and_dispersion():
    """root delay / dispersion 是 16.16 定点数，取值需与上游实际往返量级相符。

    容差取 1/65536（≈1.53e-5）—— 这是 16.16 定点数本身的量化精度，
    比它更严的断言必然失败（不是代码错，是格式精度所限）。
    """
    resp = ntp.build_response(_client_request())
    delay = struct.unpack("!I", resp[4:8])[0] / (1 << 16)
    disp = struct.unpack("!I", resp[8:12])[0] / (1 << 16)
    quantum = 1.0 / (1 << 16)
    assert abs(delay - ntp.ROOT_DELAY_SEC) <= quantum
    assert abs(disp - ntp.ROOT_DISPERSION_SEC) <= quantum


def test_build_response_reference_id_is_aiop():
    resp = ntp.build_response(_client_request())
    assert resp[12:16] == b"AIOP"


def test_build_response_transmit_receive_ref_are_consistent():
    """同一时刻构造时 receive / transmit / reference 三段应一致。"""
    resp = ntp.build_response(_client_request(), now_unix=1758456100.0)
    ref = resp[16:24]
    recv = resp[32:40]
    xmit = resp[40:48]
    assert ref == recv == xmit


def test_build_response_tolerates_short_request():
    """短报文不应抛异常（端口上会收到各种杂包）。"""
    resp = ntp.build_response(b"\x1b\x00", now_unix=1758456100.0)
    assert len(resp) == 48
    assert resp[24:32] == b"\x00" * 8


# ---------------------------------------------------------------------------
# 请求合法性判定
# ---------------------------------------------------------------------------

def test_is_valid_request_accepts_client_mode():
    assert ntp.is_valid_request(_client_request()) is True


def test_is_valid_request_rejects_short_packet():
    assert ntp.is_valid_request(b"\x1b" * 10) is False


def test_is_valid_request_rejects_non_client_mode():
    """Mode=4（服务器）/ Mode=5（广播）等不应被当成请求回包。

    否则平台会变成 NTP 反射放大器：对任意 UDP 载荷都回包，
    既能被用于 DDoS 放大，也会应答非 NTP 流量。
    """
    pkt = bytearray(48)
    pkt[0] = 0x24  # Mode=4
    assert ntp.is_valid_request(bytes(pkt)) is False
    pkt[0] = 0x25  # Mode=5
    assert ntp.is_valid_request(bytes(pkt)) is False


# ---------------------------------------------------------------------------
# 偏差计算（RFC 5905）
# ---------------------------------------------------------------------------

def test_compute_offset_perfect_clock():
    """双方时钟一致、网络对称时，offset 应为 0。"""
    offset, delay = ntp.compute_offset(req_time=0.0, resp_time=0.1,
                                       server_recv=0.05, server_transmit=0.05)
    assert abs(offset) < 1e-9
    assert abs(delay - 0.1) < 1e-9


def test_compute_offset_sign_semantics():
    """**符号方向**：offset = 上游时间 - 平台时间（RFC 5905）。

    这是最容易搞反的一处，而且搞反后运维会按错误方向调时间（越调越偏），
    所以这里把两个方向都钉死。

    构造方法：平台时钟快 1 秒时，客户端打出去的 T1/T4 比真实时刻大 1，
    服务端回填的 T2/T3 用真实时刻 → offset 为负。
    """
    # 平台时钟**快** 1 秒 → offset 为负
    offset_fast, _ = ntp.compute_offset(
        req_time=10.0, resp_time=10.02,      # 平台时钟（快 1s）
        server_recv=9.01, server_transmit=9.01,  # 上游真实时间
    )
    assert offset_fast < 0, "平台快于上游时 offset 必须为负"
    assert abs(offset_fast + 1.0) < 1e-6

    # 平台时钟**慢** 1 秒 → offset 为正
    offset_slow, _ = ntp.compute_offset(
        req_time=8.0, resp_time=8.02,        # 平台时钟（慢 1s）
        server_recv=9.01, server_transmit=9.01,
    )
    assert offset_slow > 0, "平台慢于上游时 offset 必须为正"
    assert abs(offset_slow - 1.0) < 1e-6


def test_compute_offset_zero_when_synchronized():
    """双方时钟一致时 offset 必须为 0（不能因为时延非零就漂）。"""
    offset, _ = ntp.compute_offset(0.0, 0.2, 0.05, 0.15)
    assert abs(offset) < 1e-9


def test_compute_offset_cancels_symmetric_delay():
    """对称时延不应影响 offset（这是该公式存在的意义）。"""
    o1, _ = ntp.compute_offset(0.0, 0.2, 0.05, 0.15)   # 各 50ms 时延
    o2, _ = ntp.compute_offset(0.0, 0.4, 0.05, 0.35)   # 各 100ms+ 时延
    assert abs(o1) < 1e-9
    assert abs(o2) < 1e-9


# ---------------------------------------------------------------------------
# 命令构建（默认模板 = 华哥定的 H3C 版）
# ---------------------------------------------------------------------------

def test_default_h3c_template_matches_spec():
    """默认 H3C 模板必须与定稿一致，且**不含** update-calendar。"""
    cmds = cfg.build_apply_commands("192.168.1.100")
    assert cmds == [
        "system-view",
        "clock timezone Beijing add 08:00:00",
        "clock protocol ntp",
        "ntp-service enable",
        "ntp-service unicast-server 192.168.1.100",
        "quit",
        "save force",
    ]


def test_h3c_sets_beijing_timezone_before_clock_protocol():
    """H3C 下发必须置北京时间时区，且在 clock protocol ntp **之前**。

    建模依据：H3C 出厂时区是 UTC，NTP 只同步 UTC 时刻、不带时区信息 ——
    不置时区则设备 `display clock` 仍显示 UTC，比北京时间差 8 小时，
    现场会误判成"授时没生效"。
    """
    cmds = cfg.build_apply_commands("1.1.1.1")
    assert "clock timezone Beijing add 08:00:00" in cmds
    assert cmds.index("clock timezone Beijing add 08:00:00") < cmds.index("clock protocol ntp")


def test_h3c_rollback_does_not_touch_timezone():
    """回滚**不动**时区：时区不是本次下发的副作用，撤销会改变设备原有显示口径。"""
    cmds = cfg.build_rollback_commands("1.1.1.1")
    joined = "\n".join(cmds)
    assert "clock timezone" not in joined
    assert "undo clock timezone" not in joined


def test_huawei_template_has_no_h3c_timezone_when():
    """华为模板暂不加时区命令 —— `clock timezone BJ add 08:00:00`（时区名不能带空格）
    与 H3C 写法不同，且其必要性尚未真机验证，不能照抄 H3C 那条。"""
    cmds = cfg.build_apply_commands("1.1.1.1", huawei=True)
    assert not any("clock timezone" in c for c in cmds)


def test_template_excludes_update_calendar():
    """华哥明确要求：模板里不要 `ntp-service update-calendar enable`。"""
    for huawei in (False, True):
        joined = "\n".join(cfg.build_apply_commands("1.1.1.1", huawei=huawei))
        assert "update-calendar" not in joined


def test_huawei_uses_plain_save():
    """华为没有 `save force`，必须用 `save`。"""
    cmds = cfg.build_apply_commands("1.1.1.1", huawei=True)
    assert cmds[-1] == "save"
    assert "save force" not in cmds


def test_huawei_template_matches_spec():
    """华为模板必须与华哥 2026-09-23 提供的现场定稿逐字一致。

    回归背景：此前 build_apply_commands 的华为分支里，`clock protocol ntp` 与
    `ntp-service enable` 的 if/else **两支写成了同一条命令**（注释说"华为没有"
    但代码照发），等于没做厂商区分 —— 华为设备会被下发两条不存在的命令并判失败。
    这个 bug 能溜过去，正是因为当时**没有一条测试断言华为的完整命令序列**。
    """
    cmds = cfg.build_apply_commands("192.168.124.108", huawei=True)
    assert cmds == [
        "system-view",
        "undo ntp-service disable",
        "ntp-service unicast-server 192.168.124.108",
        "quit",
        "save",
    ]


def test_huawei_omits_h3c_only_commands():
    """华为模板**不得**出现 H3C 专有命令（下发过去会 % Unrecognized command）。"""
    cmds = cfg.build_apply_commands("1.1.1.1", huawei=True)
    assert "clock protocol ntp" not in cmds
    assert "ntp-service enable" not in cmds
    assert not any(c.startswith("clock protocol") for c in cmds)
    # H3C 的 save force 同样不适用于 VRP
    assert "save force" not in cmds


def test_huawei_undoes_disable_first():
    """`undo ntp-service disable` 是华为的前提开关，必须存在且在 unicast-server 之前。

    VRP 的 ntp-service 带 disable 状态位，不先 undo 掉，
    配了 unicast-server 也不会生效（「配置全对但不同步」）。
    """
    cmds = cfg.build_apply_commands("1.1.1.1", huawei=True)
    assert "undo ntp-service disable" in cmds
    assert cmds.index("undo ntp-service disable") < cmds.index(
        "ntp-service unicast-server 1.1.1.1"
    )


def test_huawei_save_not_split_into_extra_y():
    """`save` 的 [Y/N] 交互由会话自动应答，**不能**把 Y 当成独立命令下发。

    已实测机型/版本：S5700-28C-HI(V200R001C00)、AR(VRP V500R011)。
    脚本里多发一条 `Y` 会被设备当成独立命令并报错。
    """
    cmds = cfg.build_apply_commands("1.1.1.1", huawei=True)
    assert "Y" not in cmds
    assert "y" not in cmds
    assert not any(c.strip().upper() == "Y" for c in cmds)


def test_h3c_template_unchanged_after_huawei_fix():
    """华哥明确「H3C 不变」：华为分支的修复不得影响 H3C 模板。"""
    cmds = cfg.build_apply_commands("192.168.1.100")
    assert cmds == [
        "system-view",
        "clock timezone Beijing add 08:00:00",
        "clock protocol ntp",
        "ntp-service enable",
        "ntp-service unicast-server 192.168.1.100",
        "quit",
        "save force",
    ]


def test_huawei_rollback_keeps_plain_save_and_no_clock_protocol():
    """华为回滚：只撤 unicast-server，用 `save`；不恢复 disable、不发 clock protocol。"""
    cmds = cfg.build_rollback_commands("192.168.124.108", huawei=True)
    assert cmds == [
        "system-view",
        "undo ntp-service unicast-server 192.168.124.108",
        "quit",
        "save",
    ]


def test_huawei_rollback_never_restores_disable_even_if_asked():
    """即使显式要 restore_clock_protocol，华为也不会去动 disable（华哥定的保守策略）。"""
    cmds = cfg.build_rollback_commands(
        "1.1.1.1", huawei=True, restore_clock_protocol=True
    )
    assert "ntp-service disable" not in "\n".join(cmds)
    assert "undo clock protocol" not in cmds


def test_save_false_omits_save_command():
    cmds = cfg.build_apply_commands("1.1.1.1", save=False)
    assert not any(c.startswith("save") for c in cmds)


def test_mdc_mode_emits_mdc_parameter():
    """多 MDC 设备需要 `clock protocol ntp mdc N`。"""
    cmds = cfg.build_apply_commands("1.1.1.1", clock_mode="mdc", mdc_id=2)
    assert "clock protocol ntp mdc 2" in cmds


def test_rollback_removes_server():
    cmds = cfg.build_rollback_commands("192.168.1.100")
    assert "undo ntp-service unicast-server 192.168.1.100" in cmds
    assert cmds[-1] == "save force"


def test_rollback_does_not_touch_clock_protocol_by_default():
    """默认不撤时钟协议：那是全局设置，改回去会影响设备的其它时间相关行为。"""
    cmds = cfg.build_rollback_commands("1.1.1.1")
    assert "undo clock protocol" not in cmds
    cmds2 = cfg.build_rollback_commands("1.1.1.1", restore_clock_protocol=True)
    assert "undo clock protocol" in cmds2


# ---------------------------------------------------------------------------
# 命令模板（页面可编辑：默认值在代码里，现场差异落库）
# ---------------------------------------------------------------------------

def test_render_default_templates_equals_build_helpers():
    """默认模板的渲染结果必须与 build_*_commands **逐字等价**。

    命令构建改成"模板 + 渲染"之后，两条路径等价是硬要求：页面在"没改过模板"
    的情况下预览出来的命令，必须与实际下发的完全一致 —— 否则"预览"就成了误导。
    """
    addr = "192.168.124.108"
    for huawei in (False, True):
        vendor = cfg.VENDOR_HUAWEI if huawei else cfg.VENDOR_H3C
        for mode in ("system", "mdc"):
            for save in (True, False):
                assert cfg.render_template(
                    cfg.DEFAULT_TEMPLATES[(vendor, cfg.ACTION_APPLY)],
                    addr, mode, save, 1, huawei,
                ) == cfg.build_apply_commands(addr, mode, save, huawei)
                assert cfg.render_template(
                    cfg.DEFAULT_TEMPLATES[(vendor, cfg.ACTION_ROLLBACK)],
                    addr, mode, save, 1, huawei,
                ) == cfg.build_rollback_commands(addr, mode, save, huawei)


def test_default_templates_cover_all_kinds():
    """四套模板（厂商 × 动作）必须齐备，且都带 {address} 占位符。"""
    assert set(cfg.DEFAULT_TEMPLATES) == set(cfg.TEMPLATE_KINDS)
    for kind, lines in cfg.DEFAULT_TEMPLATES.items():
        assert any(cfg.PLACEHOLDER_ADDRESS in ln for ln in lines), kind


def test_render_substitutes_address_and_mdc_id():
    lines = ["system-view", "clock protocol ntp {mdc_id}",
             "ntp-service unicast-server {address}"]
    assert cfg.render_template(lines, "10.0.0.9", "mdc", True, 3) == [
        "system-view",
        "clock protocol ntp 3",
        "ntp-service unicast-server 10.0.0.9",
    ]


def test_render_clock_protocol_placeholder_follows_mode():
    lines = [cfg.PLACEHOLDER_CLOCK_PROTOCOL]
    assert cfg.render_template(lines, "1.1.1.1", "system") == ["clock protocol ntp"]
    assert cfg.render_template(lines, "1.1.1.1", "mdc") == ["clock protocol ntp mdc 1"]


def test_render_clock_protocol_placeholder_vanishes_on_huawei():
    """华为 VRP 没有 clock protocol：占位符渲染为空 → **整行消失**，不留空行。

    留一条空行会被设备当成回车确认，是个很难查的隐蔽坑；顺带这也保证
    页面上若给华为模板写了 {clock_protocol}，不会真发一条 VRP 不认识的操作。
    """
    out = cfg.render_template(
        ["system-view", cfg.PLACEHOLDER_CLOCK_PROTOCOL, "quit"],
        "1.1.1.1", "system", True, 1, True,
    )
    assert out == ["system-view", "quit"]


def test_render_drops_save_lines_when_save_disabled():
    """取消「保存配置」时，模板里的 save 行自动剔除（自定义模板同样受勾选框控制）。

    这样勾选框对自定义模板始终有效，不会出现"勾了不保存，模板里却仍写死 save"。
    """
    lines = ["system-view",
             f"ntp-service unicast-server {cfg.PLACEHOLDER_ADDRESS}",
             "save force"]
    assert cfg.render_template(lines, "1.1.1.1", save=False) == [
        "system-view", "ntp-service unicast-server 1.1.1.1",
    ]
    assert cfg.render_template(lines, "1.1.1.1", save=True)[-1] == "save force"


def test_validate_template_body_normalizes_and_drops_blank_lines():
    lines = cfg.validate_template_body(
        "  system-view \r\n\r\n ntp-service unicast-server {address} \n\tquit\n"
    )
    assert lines == ["system-view", "ntp-service unicast-server {address}", "quit"]


@pytest.mark.parametrize("bad", [
    "",                                                              # 空模板
    "   \n  \n",                                                     # 全空白
    "system-view\nquit",                                             # 缺 {address}
    "x" * (cfg.MAX_TEMPLATE_LINE_LEN + 1),                           # 单行超长
    "\n".join([f"cmd{i}" for i in range(cfg.MAX_TEMPLATE_LINES + 1)]),  # 行数超限
    "system-view\x07\nntp-service unicast-server {address}",         # 控制字符
])
def test_validate_template_body_rejects(bad):
    with pytest.raises(ValueError):
        cfg.validate_template_body(bad)


def test_validate_template_body_rejects_missing_address_placeholder():
    """缺 {address} 必须拒绝：回验靠"回读里找该地址"，没有它永远判失败/待确认。"""
    with pytest.raises(ValueError) as ei:
        cfg.validate_template_body("system-view\nquit\nsave force")
    assert cfg.PLACEHOLDER_ADDRESS in str(ei.value)


def test_validate_template_body_accepts_typical_custom_template():
    body = ("system-view\nclock protocol ntp\n"
            "ntp-service unicast-server {address} priority\nquit\nsave force")
    lines = cfg.validate_template_body(body)
    assert lines[2] == "ntp-service unicast-server {address} priority"


def test_resolve_template_priority_override_beats_saved_beats_default():
    """模板优先级：本次下发覆盖 > 库中保存 > 内置默认。"""
    default_lines = list(cfg.DEFAULT_TEMPLATES[(cfg.VENDOR_H3C, cfg.ACTION_APPLY)])
    assert cfg.resolve_template_lines(None, cfg.VENDOR_H3C, cfg.ACTION_APPLY) == default_lines

    # 故意把 save 放最前面，与内置默认的顺序不同，便于区分来源
    saved = {(cfg.VENDOR_H3C, cfg.ACTION_APPLY):
             "system-view\nsave\nntp-service unicast-server {address}"}
    saved_lines = cfg.resolve_template_lines(saved, cfg.VENDOR_H3C, cfg.ACTION_APPLY)
    assert saved_lines != default_lines
    assert saved_lines[1] == "save"

    override = "system-view\nntp-service unicast-server {address} extra\nquit"
    out = cfg.resolve_template_lines(saved, cfg.VENDOR_H3C, cfg.ACTION_APPLY, override)
    assert out[1].endswith("{address} extra")


def test_resolve_template_rejects_invalid_saved_or_override():
    with pytest.raises(ValueError):
        cfg.resolve_template_lines(None, cfg.VENDOR_H3C, cfg.ACTION_APPLY,
                                   "system-view\nquit")


def test_vendor_of_detects_huawei_like_is_huawei():
    class _D:
        def __init__(self, vendor="", model=""):
            self.vendor, self.model = vendor, model

    assert cfg.vendor_of(_D(model="S5700")) == cfg.VENDOR_H3C
    assert cfg.vendor_of(_D(vendor="华为")) == cfg.VENDOR_HUAWEI
    assert cfg.vendor_of(_D(model="AR (VRP V500R011)")) == cfg.VENDOR_HUAWEI


async def test_apply_to_device_sends_override_template(monkeypatch):
    """下发必须真的用页面传来的模板（所见即所发），而不是回退到内置默认。"""
    captured: dict = {"cmds": []}

    class _Session:
        async def run(self, cmd, **kw):
            captured["cmds"].append(cmd)
            return ""

        def errors_in(self, out):
            return []

        async def close(self):
            pass

    async def _fake_open(device, timeout):
        return _Session()

    monkeypatch.setattr(cfg, "open_device_session", _fake_open)

    class _Dev:
        id = 1
        name = "HX-SW1"
        ip = "10.0.0.1"
        vendor = "H3C"
        model = "S5700"

    await cfg.apply_to_device(
        _Dev(), "192.168.124.108",
        override_body="system-view\nntp-service unicast-server {address}\nsave force",
    )
    assert "ntp-service unicast-server 192.168.124.108" in captured["cmds"]
    # 内置默认里的这两条**不该**出现 —— 出现即说明 override 没生效
    assert "clock protocol ntp" not in captured["cmds"]
    assert "ntp-service enable" not in captured["cmds"]


async def test_apply_to_device_picks_template_by_vendor(monkeypatch):
    """按厂商各取各的模板：H3C 的覆盖不能泄漏到华为设备上。"""
    sent: dict[str, list[str]] = {}

    class _Session:
        def __init__(self, tag):
            self.tag = tag

        async def run(self, cmd, **kw):
            sent.setdefault(self.tag, []).append(cmd)
            return ""

        def errors_in(self, out):
            return []

        async def close(self):
            pass

    async def _fake_open(device, timeout):
        return _Session(device.name)

    monkeypatch.setattr(cfg, "open_device_session", _fake_open)

    class _Dev:
        def __init__(self, name, vendor, model):
            self.id, self.name, self.vendor, self.model = 1, name, vendor, model
            self.ip = "10.0.0.1"

    h3c = _Dev("HX-SW1", "H3C", "S5700")
    hw = _Dev("HX-AR1", "华为", "AR (VRP V500R011)")

    await cfg.apply_to_device(
        h3c, "1.1.1.1",
        override_body="system-view\nntp-service unicast-server {address}\nsave force",
    )
    await cfg.apply_to_device(hw, "1.1.1.1")

    assert "save force" in sent["HX-SW1"]
    assert "save force" not in sent["HX-AR1"]
    assert "undo ntp-service disable" in sent["HX-AR1"]


async def test_command_template_roundtrip_persists_and_resets(tmp_path, monkeypatch):
    """模板保存 / 恢复默认的最小闭环（落库 + 空 body 删记录）。

    用内存 SQLite 只建 NTP 表（其它表含 PG 专属类型，SQLite 渲染不了）。
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.models.ntp import (NtpCommandTemplate, NtpDeviceConfig,
                                NtpOffsetRecord, NtpSetting)

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: NtpSetting.metadata.create_all(
                sync_conn,
                tables=[NtpSetting.__table__, NtpCommandTemplate.__table__,
                        NtpDeviceConfig.__table__, NtpOffsetRecord.__table__],
            )
        )
    Session = async_sessionmaker(engine, expire_on_commit=False)

    from app.routers import ntp as ntp_router
    from fastapi import HTTPException

    async def _noop_audit(*a, **kw):
        return None

    monkeypatch.setattr(ntp_router, "record_audit", _noop_audit)
    req = type("R", (), {"client": None, "headers": {}})()

    custom = "system-view\nntp-service unicast-server {address} priority\nquit\nsave force"
    async with Session() as db:
        res = await ntp_router.save_command_template(
            ntp_router.NtpTemplateRequest(vendor="h3c", action="apply", body=custom),
            req, db=db, user={"sub": "tester"},
        )
        assert res["reset"] is False
        assert res["item"]["customized"] is True
        assert res["item"]["body"] == custom

    async with Session() as db:
        row = (await db.execute(select(NtpCommandTemplate))).scalar_one()
        assert row.operator == "tester"
        assert "{address}" in row.body
        # 缺 {address} 的模板必须被拒（422 由 HTTPException 承载）
        with pytest.raises(HTTPException) as ei:
            await ntp_router.save_command_template(
                ntp_router.NtpTemplateRequest(vendor="h3c", action="apply",
                                              body="system-view\nquit"),
                req, db=db, user={"sub": "tester"},
            )
        assert ei.value.status_code == 422

    async with Session() as db:
        res = await ntp_router.save_command_template(
            ntp_router.NtpTemplateRequest(vendor="h3c", action="apply", body=""),
            req, db=db, user={"sub": "tester"},
        )
        assert res["reset"] is True
        assert res["item"]["customized"] is False
        # 恢复默认后，值等于内置默认
        assert res["item"]["body"] == cfg.default_template_body("h3c", "apply")

    async with Session() as db:
        assert (await db.execute(select(NtpCommandTemplate))).scalars().all() == []

    await engine.dispose()


def test_server_time_fields_shape():
    """顶部实时时钟要的字段：epoch + 宿主时区偏移（分钟）。"""
    import time

    from app.routers import ntp as ntp_router

    f = ntp_router._server_time_fields()
    assert abs(f["server_time_epoch"] - time.time()) < 30
    assert isinstance(f["server_timezone_offset_minutes"], int)
    assert -12 * 60 <= f["server_timezone_offset_minutes"] <= 14 * 60
    assert f["server_time"]


# ---------------------------------------------------------------------------
# 地址校验（防命令注入）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "1.1.1.1\nquit",          # 换行拆命令 → 可在设备上执行任意命令
    "1.1.1.1\r\nsave",
    "1.1.1.1; save",
    "1.1.1.1 && save",
    "1.1.1.1 | save",
    "1.1.1.1`save`",
    "$(save)",
    "1.1.1.1 1.1.1.2",        # 空格拆参数
    "",
    "   ",
])
def test_validate_address_rejects_injection(bad):
    """NTP 地址会被拼进下发到设备的命令行 —— 必须拦住注入字符。

    这是真实的命令注入面：不校验就能用 `1.1.1.1\\nquit\\nsave` 在设备上任意执行。
    """
    with pytest.raises(ValueError):
        cfg.validate_address(bad)


@pytest.mark.parametrize("good", [
    "192.168.1.100",
    "10.0.0.1",
    "ntp.aliyun.com",
    "time1.example.com",
    "2001:db8::1",
])
def test_validate_address_accepts_valid(good):
    assert cfg.validate_address(good) == good


def test_validate_address_rejects_oversize():
    with pytest.raises(ValueError):
        cfg.validate_address("a" * 65 + ".com")


def test_validate_clock_mode():
    assert cfg.validate_clock_mode("SYSTEM") == "system"
    assert cfg.validate_clock_mode("mdc") == "mdc"
    with pytest.raises(ValueError):
        cfg.validate_clock_mode("bogus")


# ---------------------------------------------------------------------------
# 回读解析与回验判定
# ---------------------------------------------------------------------------

def test_extract_ntp_servers_from_config():
    text = (
        " clock protocol ntp\n"
        " ntp-service unicast-server 192.168.1.100\n"
        " ntp-service unicast-server 10.0.0.5 priority\n"
    )
    servers = cfg._extract_ntp_servers(text)
    assert "192.168.1.100" in servers
    assert "10.0.0.5" in servers


def test_has_server_matches_case_insensitively():
    snap = cfg.NtpSnapshot("ntp-service unicast-server NTP.Aliyun.com",
                           "display current-configuration | include ntp", True)
    assert cfg._has_server(snap, "ntp.aliyun.com")


def test_has_server_false_when_absent():
    snap = cfg.NtpSnapshot("ntp-service unicast-server 10.0.0.1", "cmd", True)
    assert not cfg._has_server(snap, "192.168.1.100")


def test_judge_apply_applied_when_server_present():
    after = cfg.NtpSnapshot(
        " ntp-service unicast-server 192.168.1.100",
        "display current-configuration | include ntp", True,
        servers=["192.168.1.100"],
    )
    state, msg = cfg._judge_apply("192.168.1.100", after, [])
    assert state == "applied"
    assert msg is None


def test_judge_apply_failed_when_readback_trusted_but_missing():
    after = cfg.NtpSnapshot(
        " ntp-service unicast-server 10.0.0.1",
        "display current-configuration | include ntp", True,
        servers=["10.0.0.1"],
    )
    state, msg = cfg._judge_apply("192.168.1.100", after, [])
    assert state == "failed"
    assert "192.168.1.100" in msg


def test_judge_apply_unverified_when_readback_untrusted():
    """**核心语义**：命令没报错但回读不可信 → unverified，绝不判 failed。

    判 failed 会让操作人以为没生效而重复下发生产设备；实际可能已经配上了。
    """
    after = cfg.NtpSnapshot("", "display current-configuration | include ntp",
                            False, notes=["无输出"])
    state, msg = cfg._judge_apply("192.168.1.100", after, [])
    assert state == "unverified"
    assert "人工确认" in msg


def test_judge_apply_failed_on_command_error():
    after = cfg.NtpSnapshot("", "cmd", False, notes=["不要紧"])
    state, msg = cfg._judge_apply("192.168.1.100", after, ["`clock protocol ntp` → % Unrecognized command"])
    assert state == "failed"
    assert "报错" in msg


def test_judge_rollback_success():
    after = cfg.NtpSnapshot(" ntp-service unicast-server 10.0.0.1", "cmd", True,
                            servers=["10.0.0.1"])
    state, msg = cfg._judge_rollback("192.168.1.100", after, [])
    assert state == "rolled_back"
    assert msg is None


def test_judge_rollback_failed_if_still_present():
    after = cfg.NtpSnapshot(" ntp-service unicast-server 192.168.1.100", "cmd", True,
                            servers=["192.168.1.100"])
    state, _msg = cfg._judge_rollback("192.168.1.100", after, [])
    assert state == "failed"


def test_judge_rollback_unverified_when_untrusted():
    after = cfg.NtpSnapshot("", "cmd", False, notes=["读不到"])
    state, _msg = cfg._judge_rollback("192.168.1.100", after, [])
    assert state == "unverified"


# ---------------------------------------------------------------------------
# 状态类命令的空回读也可信（与配置类命令的区别）
# ---------------------------------------------------------------------------

def test_snapshot_kind_distinguishes_state_and_config():
    state_snap = cfg.NtpSnapshot("", "display ntp-service status", True, kind="state")
    config_snap = cfg.NtpSnapshot("", "display current-configuration | include ntp",
                                  False, kind="config")
    assert state_snap.trusted and state_snap.kind == "state"
    assert not config_snap.trusted and config_snap.kind == "config"


def test_is_state_cmd():
    assert cfg._is_state_cmd("display ntp-service status") is True
    assert cfg._is_state_cmd("display ntp status") is True
    assert cfg._is_state_cmd("display current-configuration | include ntp") is False


# ---------------------------------------------------------------------------
# 手动同步的安全约束
# ---------------------------------------------------------------------------

def test_set_system_time_rejects_large_offset():
    """偏差过大必须拒绝：那是配置错误（上游/时区/宿主 NTP），不是漂移。"""
    ok, msg = ntp.set_system_time(9999.0)
    assert ok is False
    assert "超出" in msg or "上限" in msg


def test_set_system_time_error_message_is_actionable():
    """拒绝时要说清楚"先去排查根因"，而不是只报一个数字。"""
    _ok, msg = ntp.set_system_time(-99999.0)
    assert "排查" in msg


def _force_platform(monkeypatch, os_name: str, system: str) -> None:
    """把 ``set_system_time`` 内的平台判定固定成指定值。

    它是在**函数体内** ``import os / platform`` 的，所以 patch 全局模块属性即可生效。
    这也是唯一能在开发机上安全测 Windows 分支的办法 —— 真去跑 ``w32tm /resync``
    会动到本机时钟。
    """
    monkeypatch.setattr("os.name", os_name)
    monkeypatch.setattr("platform.system", lambda: system)


def test_step_prefers_inprocess_clock_settime(monkeypatch):
    """Linux 首选内置 clock_settime：不依赖外部工具，也不必以 root 跑整个后端。"""
    _force_platform(monkeypatch, "posix", "Linux")

    called = []

    def _fake_run(cmd, timeout=20):
        called.append(cmd)
        return False, "不该被调用"

    monkeypatch.setattr(ntp, "_step_clock_inprocess", lambda t: (True, "内置接口 clock_settime"))
    monkeypatch.setattr(ntp, "_run_step_cmd", _fake_run)

    ok, msg = ntp.set_system_time(-0.002)
    assert ok is True
    assert "clock_settime" in msg
    assert called == []          # 第一条成功就不该再试后面的策略


def test_step_falls_back_to_date_when_no_capability(monkeypatch):
    """没有 CAP_SYS_TIME 时应退到 date -s，且目标时刻按「当前 + 偏差」算（方向不能反）。"""
    import time as _time

    _force_platform(monkeypatch, "posix", "Linux")
    monkeypatch.setattr(
        ntp, "_step_clock_inprocess",
        lambda t: (False, "无权限：Operation not permitted（进程缺少 CAP_SYS_TIME）"),
    )

    seen = []

    def _fake_run(cmd, timeout=20):
        seen.append(cmd)
        return cmd[0] == "date", "执行成功" if cmd[0] == "date" else "退出码 1"

    monkeypatch.setattr(ntp, "_run_step_cmd", _fake_run)

    ok, msg = ntp.set_system_time(0.5)
    assert ok is True
    assert "date -s" in msg
    assert seen[0][0] == "date"
    # "@<epoch>"：offset>0 表示上游快（平台慢）→ 目标时刻应在**未来**
    target = float(seen[0][2][1:])
    assert abs(target - (_time.time() + 0.5)) < 5


def test_step_failure_message_lists_every_attempt(monkeypatch):
    """全部失败时必须逐条给出真实原因。

    回归背景（2026-09-24 真机）：.108 上 ``chronyc`` **存在**，但 ``chronyc makestep``
    返回 "501 Not authorised"（chronyd 只允许 root / chrony 用户经 Unix socket 控制，
    走网络一律拒绝）。旧实现把所有失败都归成"未找到可用的时间同步工具…请安装 chrony"，
    结果让人去装一个已经装好的软件 —— 文案比故障本身更费时间。
    """
    _force_platform(monkeypatch, "posix", "Linux")
    monkeypatch.setattr(
        ntp, "_step_clock_inprocess",
        lambda t: (False, "无权限：Operation not permitted（进程缺少 CAP_SYS_TIME）"),
    )

    def _fake_run(cmd, timeout=20):
        if cmd[0] == "chronyc":
            return False, "退出码 1：501 Not authorised"
        return False, "退出码 1：date: cannot set date: Operation not permitted"

    monkeypatch.setattr(ntp, "_run_step_cmd", _fake_run)

    ok, msg = ntp.set_system_time(-0.002)
    assert ok is False
    for name in ("clock_settime", "date -s", "chronyc makestep"):
        assert name in msg, f"诊断里缺了策略 {name}"
    assert "501 Not authorised" in msg        # 原始原因要带出来
    assert "CAP_SYS_TIME" in msg              # 并给出可行的补救方向
    # 旧文案的误导性结论绝不能再出现
    assert "未找到可用的时间同步工具" not in msg
    assert "请安装 chrony" not in msg


def test_step_survives_strategy_exception(monkeypatch):
    """单条策略自身抛异常时，不能带崩整个校时流程。"""
    _force_platform(monkeypatch, "posix", "Linux")

    def _boom(_t):
        raise RuntimeError("explode")

    monkeypatch.setattr(ntp, "_step_clock_inprocess", _boom)
    monkeypatch.setattr(ntp, "_run_step_cmd", lambda cmd, timeout=20: (True, "执行成功"))

    ok, msg = ntp.set_system_time(0.0)
    assert ok is True
    assert "date -s" in msg


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_step_rejects_non_finite_offset(bad):
    """非有限值会让 `abs(x) > 上限` 恒为 False 而绕过安全阀，必须显式拦掉。"""
    ok, msg = ntp.set_system_time(bad)
    assert ok is False
    assert "非法" in msg


def test_step_windows_prefers_w32tm_then_setdate(monkeypatch):
    """Windows：w32tm 失败时要退到 Set-Date，并按**实测偏差**校正。"""
    _force_platform(monkeypatch, "nt", "Windows")

    seen = []

    def _fake_run(cmd, timeout=20):
        seen.append(cmd)
        if cmd[0] == "powershell":
            return True, "执行成功"
        return False, "退出码 1：The computer did not resync because no time data was available."

    monkeypatch.setattr(ntp, "_run_step_cmd", _fake_run)

    ok, msg = ntp.set_system_time(-0.25)
    assert ok is True
    assert "Set-Date" in msg
    assert seen[0][0] == "w32tm"
    # 用我们实测的偏差，而不是让系统自己去猜源
    assert "AddSeconds(-0.25)" in seen[1][-1]


def test_step_fail_hint_is_platform_specific():
    """失败处置建议必须按平台给 —— Windows 上不能建议去配 systemd。

    2026-09-24 本地 Windows 实测：两条策略全失败时，文案里混着
    systemd / CAP_SYS_TIME / chronyc / date -s，等于指了一条走不通的路
    （和"让人去装一个已经装好的 chrony"是同一类问题）。
    """
    lin = ntp._step_fail_hint(False)
    win = ntp._step_fail_hint(True)

    assert "CAP_SYS_TIME" in lin and "chronyc" in lin
    for kw in ("CAP_SYS_TIME", "systemd", "AmbientCapabilities", "chronyc", "date -s"):
        assert kw not in win, f"Windows 失败建议里不该出现 {kw}"
    assert "w32time" in win and "Set-Date" in win
    # 两个平台都要先说清"平台不自行提权"这条边界
    assert "提权" in lin and "提权" in win


def test_step_failure_classifies_common_causes(monkeypatch):
    """逐条原因前要带一句人话结论；认不出的原样保留（绝不硬套）。"""
    assert ntp._classify_fail("发生下列错误: 服务尚未启动。 (0x80070426)") == "Windows Time 服务未启动"
    assert ntp._classify_fail("Set-Date : 客户端没有所需的特权。") == "进程权限不足，无法修改系统时钟"
    cause = ntp._classify_fail("501 Not authorised")
    assert cause and "chronyd" in cause
    assert ntp._classify_fail("完全认不出的错误") is None

    # 端到端：Windows 失败文案里要真的出现【…】结论
    _force_platform(monkeypatch, "nt", "Windows")
    monkeypatch.setattr(
        ntp, "_run_step_cmd",
        lambda cmd, timeout=20: (False, "发生下列错误: 服务尚未启动。 (0x80070426)"),
    )
    ok, msg = ntp.set_system_time(0.0)
    assert ok is False
    assert "【Windows Time 服务未启动】" in msg
    assert "systemd" not in msg


def test_inprocess_step_helper_is_safe_on_any_platform():
    """内置校时在任何平台都要"返回结果"，不能抛异常（Windows 上无此接口）。"""
    import time as _time

    ok, detail = ntp._step_clock_inprocess(_time.time())
    assert isinstance(ok, bool)
    assert detail


async def test_manual_sync_reports_residual(monkeypatch):
    """校正后要复核残差 —— "改过时钟"不等于"时钟就对齐了"。"""
    def _no_session():
        raise RuntimeError("unit test 不连库")

    monkeypatch.setattr("app.database.async_session", _no_session)

    async def _fake_sample(server, count=1):
        return 0.0004, 0.001

    async def _fake_record(offset, delay, server, source="upstream",
                           error=None, success=True):
        return None

    monkeypatch.setattr(ntp, "sample_upstream", _fake_sample)
    monkeypatch.setattr(ntp, "set_system_time", lambda off: (True, "已校正宿主时钟 -0.002 秒（内置接口 clock_settime）。"))
    monkeypatch.setattr(ntp, "record_offset", _fake_record)

    res = await ntp.manual_sync("ntp.aliyun.com")
    assert res["success"] is True
    assert res["residual_seconds"] == pytest.approx(0.0004)
    assert "复核残差" in res["message"]


async def test_manual_sync_skips_residual_when_step_failed(monkeypatch):
    """校正失败就不该再谈残差，直接如实回报失败原因。"""
    def _no_session():
        raise RuntimeError("unit test 不连库")

    monkeypatch.setattr("app.database.async_session", _no_session)

    async def _fake_sample(server, count=1):
        return -0.0015, 0.002

    async def _fake_record(offset, delay, server, source="upstream",
                           error=None, success=True):
        return None

    monkeypatch.setattr(ntp, "sample_upstream", _fake_sample)
    monkeypatch.setattr(ntp, "set_system_time", lambda off: (False, "未能校正宿主时钟：无权限"))
    monkeypatch.setattr(ntp, "record_offset", _fake_record)

    res = await ntp.manual_sync("ntp.aliyun.com")
    assert res["success"] is False
    assert res["residual_seconds"] is None
    assert "无权限" in res["message"]
    assert "复核残差" not in res["message"]


# ---------------------------------------------------------------------------
# 开关语义
# ---------------------------------------------------------------------------

def test_server_disabled_by_default_after_reset():
    """两个开关默认都关闭（设计要求：按需手动开启）。"""
    ntp.reset_state()
    from app.config import settings
    # 总闸默认打开（允许页面开），但运行时开关默认关闭
    assert ntp._runtime_server_enabled is False
    assert ntp._runtime_upstream_enabled is False
    # server_enabled() 是「总闸 且 页面开关」，总闸开着但页面开关关着 → False
    assert ntp.server_enabled() is False
    assert ntp.upstream_enabled() is False
    assert settings.NTP_SERVER_ENABLED is True


def test_default_ntp_settings_disabled():
    """模型层默认值也必须是 False（双保险）。"""
    from app.models.ntp import NtpSetting
    col = NtpSetting.__table__.c.server_enabled
    assert col.default.arg is False
    col2 = NtpSetting.__table__.c.upstream_enabled
    assert col2.default.arg is False


def test_effective_upstream_server_falls_back_to_env():
    """设置行留空时回退到 .env 默认值（不把默认地址固化成历史数据）。"""
    from app.config import settings

    class _Row:
        upstream_server = None

    assert ntp.effective_upstream_server(_Row()) == settings.NTP_UPSTREAM_SERVER
    assert ntp.effective_upstream_server(None) == settings.NTP_UPSTREAM_SERVER

    class _Row2:
        upstream_server = "192.168.9.9"

    assert ntp.effective_upstream_server(_Row2()) == "192.168.9.9"


# ---------------------------------------------------------------------------
# 回归：开启后必须真正持有端口（.108 真机踩过的坑）
# ---------------------------------------------------------------------------

async def test_server_loop_binds_port_and_holds_it(monkeypatch):
    """开启开关后，应答循环必须**持续**持有端口，而不是绑上就立刻释放。

    .108 真机事故：循环体写成「绑定成功后 `await ev.wait()`」，而事件此刻是 set
    状态（正因为"已开启"才走到这一步）→ wait() 立即返回 → finally 关掉刚绑的端口
    → 循环回头再绑 …… 表现为 `server_enabled=True` 但 `bound=False`、123 端口
    始终不监听、CPU 空转。

    这里用高位端口跑真实循环，断言绑定确实发生了、且**稳定保持**一段时间。
    """
    import asyncio
    import socket

    from app.config import settings

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()

    ntp.reset_state()
    monkeypatch.setattr(settings, "NTP_LISTEN_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "NTP_PORT", free_port)
    monkeypatch.setattr(ntp, "PORT_HOLD_POLL", 0.05)

    ntp.set_server_enabled(True)
    task = asyncio.create_task(ntp.ntp_server_loop())
    try:
        # 等绑定完成
        for _ in range(100):
            await asyncio.sleep(0.02)
            if ntp._stats["bound_port"] == free_port:
                break
        assert ntp._stats["bound_port"] == free_port, "开启后未能绑定端口"

        # 关键：持续观察一段时间，端口必须始终被持有（不能反复绑/解）
        disconnected = 0
        for _ in range(20):
            await asyncio.sleep(0.05)
            if ntp._stats["bound_port"] != free_port:
                disconnected += 1
        assert disconnected == 0, "端口被反复绑定/释放（循环体在空转）"

        # 真正能收到应答（证明 transport 还活着）
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        class _Cli(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                if not fut.done():
                    fut.set_result(data)

        ct, _ = await loop.create_datagram_endpoint(
            _Cli, remote_addr=("127.0.0.1", free_port))
        req = bytearray(48)
        req[0] = 0x1B
        ct.sendto(bytes(req))
        data = await asyncio.wait_for(fut, timeout=2)
        ct.close()
        assert len(data) == 48 and data[0] == 0x24
    finally:
        ntp.set_server_enabled(False)
        # 关闭后循环应在有限时间内退出并释放端口
        for _ in range(100):
            await asyncio.sleep(0.05)
            if ntp._stats["bound_port"] is None:
                break
        assert ntp._stats["bound_port"] is None, "关闭开关后端口未释放"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        ntp.reset_state()


async def test_server_loop_does_not_bind_when_disabled(monkeypatch):
    """关闭态绝不占用端口（默认关闭的设计要求）。"""
    import asyncio

    from app.config import settings

    ntp.reset_state()
    monkeypatch.setattr(settings, "NTP_LISTEN_HOST", "127.0.0.1")
    monkeypatch.setattr(ntp, "PORT_HOLD_POLL", 0.05)

    assert ntp.server_enabled() is False
    task = asyncio.create_task(ntp.ntp_server_loop())
    try:
        await asyncio.sleep(0.3)
        assert ntp._stats["bound_port"] is None, "关闭态不应绑定端口"
        assert ntp._stats["bind_error"] is None
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        ntp.reset_state()


async def test_upstream_loop_does_not_spin_when_enabled(monkeypatch):
    """开启上行取时后，不能在采样周期内疯狂重复采样。

    同一处坑的另一半：`wait_for(ev.wait(), 300)` 在开启态（事件已 set）会立即返回，
    退化成紧密循环对上游疯狂发包。
    """
    import asyncio

    calls = {"n": 0}

    async def _fake_sample(server, count=1):
        calls["n"] += 1
        return 0.001, 0.005

    async def _fake_record(*a, **kw):
        return None

    ntp.reset_state()
    monkeypatch.setattr(ntp, "sample_upstream", _fake_sample)
    monkeypatch.setattr(ntp, "record_offset", _fake_record)
    monkeypatch.setattr(ntp, "PORT_HOLD_POLL", 0.05)
    monkeypatch.setattr(ntp, "UPSTREAM_POLL_INTERVAL", 2.5)

    ntp.set_upstream_enabled(True)
    task = asyncio.create_task(ntp.ntp_upstream_loop())
    try:
        await asyncio.sleep(1.0)
        # 1 秒内最多采样 1~2 次；若空转会是几十上百次
        assert calls["n"] <= 2, f"上行循环在空转（1 秒内采样 {calls['n']} 次）"
    finally:
        ntp.set_upstream_enabled(False)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        ntp.reset_state()


async def test_toggle_request_persists_switch_state(tmp_path, monkeypatch):
    """开关必须**落库**，否则重启后"关了又自己开"（或反过来"开了又变关"）。

    .108 真机踩过：PUT 时新建的设置行被硬编码成 `server_enabled=False`，
    而请求里的 `server_enabled=true` 只改了运行态、没写进库 → 接口返回"已开启"、
    重启后却变回关闭，运维会以为开关失效。

    这里用内存 SQLite **只建 NTP 表**后直接调用路由函数（不建全库：其它表用了
    PG 专属的 ARRAY 类型，SQLite 渲染不了），断言库里的值与请求一致。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.models.ntp import NtpDeviceConfig, NtpOffsetRecord, NtpSetting

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    # 只建这三张表：避免牵连其它表的 PG 专属类型（threat_intel.tags 是 ARRAY）
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: NtpSetting.metadata.create_all(
                sync_conn, tables=[NtpSetting.__table__, NtpDeviceConfig.__table__,
                                   NtpOffsetRecord.__table__]
            )
        )
    Session = async_sessionmaker(engine, expire_on_commit=False)

    from app.routers import ntp as ntp_router

    ntp.reset_state()
    monkeypatch.setattr(ntp_router.settings, "NTP_SERVER_ENABLED", True)
    monkeypatch.setattr(ntp_router.settings, "NTP_UPSTREAM_ENABLED", True)
    monkeypatch.setattr(ntp, "set_server_enabled", lambda v: v)
    monkeypatch.setattr(ntp, "set_upstream_enabled", lambda v: v)

    async def _noop_audit(*a, **kw):
        return None

    monkeypatch.setattr(ntp_router, "record_audit", _noop_audit)

    async with Session() as db:
        body = ntp_router.NtpToggleRequest(server_enabled=True, upstream_enabled=False)
        req = type("R", (), {"client": None, "headers": {}})()
        await ntp_router.update_ntp_settings(
            body, req, db=db, user={"sub": "tester"})

    async with Session() as db:
        from sqlalchemy import select
        row = (await db.execute(select(NtpSetting))).scalar_one()
        assert row.server_enabled is True, "server_enabled 未落库（重启后会丢）"
        assert row.upstream_enabled is False
        assert row.operator == "tester"

    await engine.dispose()
    ntp.reset_state()


# ---------------------------------------------------------------------------
# 告警引擎接入
# ---------------------------------------------------------------------------

def test_clock_offset_registered_in_supported_metrics():
    from app.services import alert_rule_engine as eng
    assert "clock_offset" in eng.SUPPORTED_METRICS


def test_clock_offset_seed_rule_exists_and_disabled_by_default():
    """时钟偏差种子规则存在，但**默认关闭**。

    它会在每台在线设备上各产生一条告警（平台级指标挂在设备维度），
    默认开启会刷屏并把真正重要的告警挤出视野。
    """
    from app import database as db_mod
    clock_rules = [r for r in db_mod.DEFAULT_ALERT_RULES if r[1] == "clock_offset"]
    assert len(clock_rules) == 1
    name, _metric, cond, threshold, _dur, _sev, _desc = clock_rules[0]
    assert cond == "gt"
    assert threshold == 60, "阈值必须是 60 秒（设计要求不超过 60 秒）"
    assert name in db_mod.DISABLED_SEED_RULES


def test_clock_offset_message_says_platform_not_device():
    """告警文案必须点明是**平台**问题，否则运维会跑去查设备。"""
    from app.services import alert_rule_engine as eng

    class _Dev:
        name = "SW-1"
        ip = "10.0.0.1"

    class _Rule:
        name = "平台时钟偏差过大"
        metric = "clock_offset"
        threshold = 60
        duration = 120

    msg = eng._scalar_message(_Dev(), _Rule(), 75.5)
    assert "平台" in msg
    assert "非设备" in msg or "不是设备" in msg


# ---------------------------------------------------------------------------
# 近期请求环形缓冲（下行授时：哪几台来取过时 / 实际轮询间隔是多少）
# ---------------------------------------------------------------------------
#
# 这几条守的是"现场可读性"：单值 last_client 只能回答"刚有没有人来过"，
# 而那三个真正要命的问题（哪几台来了 / 间隔多少 / 谁压根没来）全靠这个列表。
# 缓冲一旦被写坏（不封顶、间隔算错源、非法包被静默丢弃），页面上是**看不出**的。

def _fake_clock(monkeypatch, start_epoch: float = 1758456000.0):
    """把 ``ntp._now`` 换成可控时钟，返回「推进 N 秒」的函数。

    间隔断言必须能精确控制时间：依赖真实时钟就只能写 ``>= 0`` 这种等于没断言的断言，
    而"同源相邻两条之差"恰恰是最容易算错的地方（算成跨源、算成全量第一条）。
    """
    state = {"t": start_epoch}

    def _now():
        return datetime.fromtimestamp(state["t"], tz=timezone.utc)

    monkeypatch.setattr(ntp, "_now", _now)

    def advance(seconds: float) -> None:
        state["t"] += seconds

    return advance


def test_recent_requests_records_answered_request():
    ntp.reset_state()
    ntp.handle_request(_client_request(), "192.168.124.69")

    items = ntp.recent_requests()
    assert len(items) == 1
    r = items[0]
    assert r["client_ip"] == "192.168.124.69"
    assert r["answered"] is True
    assert r["reason"] is None
    assert r["at"], "必须带时刻，否则页面上排不了序、也算不了间隔"
    # 同源首条没有可参照的前序 → 必须是 None，不能编一个 0 出来
    # （0 会被读成"瞬间连发"，直接把排查引向错误方向）
    assert r["interval_seconds"] is None


def test_recent_requests_invalid_packet_recorded_as_unanswered():
    """非法报文也要入列并写明原因。

    "设备在发、平台不回"是最该立刻看见的故障；若因为"没应答成功"就不记录，
    这个故障永远只体现在一个 counter 上，没人看得出是哪台设备。
    """
    ntp.reset_state()
    ntp.handle_request(b"\x00" * 48, "10.0.0.9")

    r = ntp.recent_requests()[0]
    assert r["client_ip"] == "10.0.0.9"
    assert r["answered"] is False
    assert "非法请求" in r["reason"]


def test_recent_requests_interval_is_same_source_delta(monkeypatch):
    """间隔 = 与**同源**上一条的时间差；中间夹别的设备不得污染计算结果。"""
    ntp.reset_state()
    advance = _fake_clock(monkeypatch)

    ntp.handle_request(_client_request(), "10.0.0.1")
    advance(64)
    ntp.handle_request(_client_request(), "10.0.0.2")   # 干扰项：不同源
    advance(64)
    ntp.handle_request(_client_request(), "10.0.0.1")

    items = ntp.recent_requests()
    assert items[0]["client_ip"] == "10.0.0.1"
    assert items[0]["interval_seconds"] == pytest.approx(128.0, abs=0.01)
    # 10.0.0.2 是首条 → 无间隔可比
    assert items[1]["client_ip"] == "10.0.0.2"
    assert items[1]["interval_seconds"] is None


def test_recent_requests_is_bounded_and_drops_oldest():
    """定长环形缓冲：超上限丢最旧，内存不随运行时长增长。"""
    ntp.reset_state()
    total = ntp.RECENT_REQUESTS_MAX + 5
    for i in range(total):
        ntp.handle_request(_client_request(), f"10.0.0.{i}")

    items = ntp.recent_requests()
    assert len(items) == ntp.RECENT_REQUESTS_MAX
    # 最新在前
    assert items[0]["client_ip"] == f"10.0.0.{total - 1}"
    assert items[-1]["client_ip"] == f"10.0.0.{total - ntp.RECENT_REQUESTS_MAX}"
    assert all(r["client_ip"] != "10.0.0.0" for r in items), "最旧的应已挤出"


def test_recent_requests_limit_argument_keeps_newest():
    ntp.reset_state()
    for i in range(5):
        ntp.handle_request(_client_request(), f"10.0.0.{i}")

    items = ntp.recent_requests(limit=2)
    assert [r["client_ip"] for r in items] == ["10.0.0.4", "10.0.0.3"]


def test_recent_requests_records_build_failure(monkeypatch):
    """构造应答抛异常时留下「未应答 + 原因」，而不是静默丢弃。"""
    ntp.reset_state()

    def _boom(*_a, **_kw):
        raise ValueError("bad request")

    monkeypatch.setattr(ntp, "build_response", _boom)
    assert ntp.handle_request(_client_request(), "10.0.0.7") is None

    r = ntp.recent_requests()[0]
    assert r["answered"] is False
    assert "构造应答失败" in r["reason"]


def test_server_status_exposes_recent_requests_newest_first():
    """状态接口必须把列表和上限一起给出（前端据此渲染表头与"本次启动以来"口径）。"""
    ntp.reset_state()
    ntp.handle_request(_client_request(), "10.0.0.1")
    ntp.handle_request(_client_request(), "10.0.0.2")

    st = ntp.server_status()
    assert st["recent_requests_max"] == ntp.RECENT_REQUESTS_MAX
    assert [r["client_ip"] for r in st["recent_requests"]] == ["10.0.0.2", "10.0.0.1"]
    # 计数与列表同源同生命周期：都是"本次启动以来"
    assert st["requests"] == 2 and st["responses"] == 2


def test_reset_state_clears_recent_requests():
    """reset_state 必须清空缓冲 —— 否则测试间互相污染，且"重启后归零"的语义不成立。"""
    ntp.reset_state()
    ntp.handle_request(_client_request(), "10.0.0.1")
    assert ntp.recent_requests() and ntp._source_stats

    ntp.reset_state()
    assert ntp.recent_requests() == []
    assert ntp._source_stats == {}


def test_interval_survives_display_buffer_eviction(monkeypatch):
    """间隔不受列表容量影响：来源被挤出列表后，下次回来仍能算出真实间隔。

    这是 .108 实测踩到的场景：一批设备处于启动突发，1 秒一发就能把 20 条填满。
    若间隔直接从列表回看，其余设备（64s 起步）的间隔会**永远**显示 '-' ——
    页面上看起来像"没数据"，实际是算法被一台勤快的设备挤掉了，最容易被误读。
    """
    ntp.reset_state()
    advance = _fake_clock(monkeypatch)

    ntp.handle_request(_client_request(), "10.0.0.1")
    # 用别的来源把 10.0.0.1 那条彻底挤出展示缓冲
    for i in range(ntp.RECENT_REQUESTS_MAX):
        ntp.handle_request(_client_request(), f"10.9.9.{i}")
    assert all(r["client_ip"] != "10.0.0.1" for r in ntp.recent_requests())

    advance(64)
    ntp.handle_request(_client_request(), "10.0.0.1")
    assert ntp.recent_requests()[0]["interval_seconds"] == pytest.approx(64.0, abs=0.01)


def test_interval_never_negative_on_clock_step_back():
    """时钟回拨时不得给出负间隔。

    NTP 场景下宿主时钟被往回拨是真会发生的（手动校时、上游同步），
    此时负值会让页面显示成怪数字，比显示 '-' 更糟。
    """
    ntp.reset_state()
    ntp.handle_request(_client_request(), "10.0.0.1")
    # 直接把上一条记成"未来"的时刻来模拟回拨，不依赖真实时钟
    ntp._source_stats["10.0.0.1"]["_ts"] += 3600
    ntp.handle_request(_client_request(), "10.0.0.1")

    iv = ntp.recent_requests()[0]["interval_seconds"]
    assert iv is not None and iv >= 0, f"间隔不得为负，实际 {iv}"


def test_tracked_sources_is_bounded():
    """被扫描/杂包刷 IP 时，台账不无限增长（UDP 无连接，谁都能发）。"""
    ntp.reset_state()
    for i in range(ntp.MAX_TRACKED_SOURCES + 50):
        ntp.handle_request(_client_request(), f"10.{i // 256}.{i % 256}.1")

    assert len(ntp._source_stats) <= ntp.MAX_TRACKED_SOURCES


# ---------------------------------------------------------------------------
# 来源台账：这是"哪几台真的来取过时"的权威答案（不受 20 条窗口影响）
# ---------------------------------------------------------------------------

def test_recent_sources_counts_first_and_last(monkeypatch):
    ntp.reset_state()
    advance = _fake_clock(monkeypatch)

    ntp.handle_request(_client_request(), "10.0.0.1")
    advance(64)
    ntp.handle_request(_client_request(), "10.0.0.1")

    s = ntp.recent_sources()
    assert len(s) == 1
    assert s[0]["client_ip"] == "10.0.0.1"
    assert s[0]["count"] == 2
    assert s[0]["answered"] == 2
    assert s[0]["first_at"] < s[0]["last_at"], "首次/最近必须在两端"
    assert s[0]["interval_seconds"] == pytest.approx(64.0, abs=0.01)
    # 内部字段不得外泄（"_ts" 是 epoch 浮点，页面拿它没用还会误导口径）
    assert "_ts" not in s[0]


def test_recent_sources_remembers_evicted_sources():
    """**核心断言**：来源被挤出 20 条窗口后，台账里依然在。

    这正是"关心的那几台华为到底来没来"能被可信回答的前提 —— 否则一台 64 秒前
    来过的设备会被读成"从没来过"，把排查直接带向错误方向（去查设备配置/ACL）。
    """
    ntp.reset_state()
    ntp.handle_request(_client_request(), "192.168.124.69")   # 关心的设备，早早就来过
    for i in range(ntp.RECENT_REQUESTS_MAX * 2):              # 被突发设备挤出窗口
        ntp.handle_request(_client_request(), f"10.9.9.{i % 250}")

    assert all(r["client_ip"] != "192.168.124.69" for r in ntp.recent_requests())
    ips = [s["client_ip"] for s in ntp.recent_sources()]
    assert "192.168.124.69" in ips, "台账不得因窗口淘汰而丢失来源"


def test_recent_sources_counts_unanswered_separately():
    """未应答要单独计数：设备在发、平台不回是必须能一眼看出的故障。"""
    ntp.reset_state()
    ntp.handle_request(_client_request(), "10.0.0.1")
    ntp.handle_request(b"\x00" * 48, "10.0.0.1")

    s = ntp.recent_sources()[0]
    assert s["count"] == 2 and s["answered"] == 1


def test_recent_sources_sorted_by_last_request_desc():
    ntp.reset_state()
    ntp.handle_request(_client_request(), "10.0.0.1")
    ntp.handle_request(_client_request(), "10.0.0.2")

    assert [s["client_ip"] for s in ntp.recent_sources()] == ["10.0.0.2", "10.0.0.1"]


def test_server_status_exposes_recent_sources():
    ntp.reset_state()
    ntp.handle_request(_client_request(), "10.0.0.1")

    st = ntp.server_status()
    assert st["recent_sources_max"] == ntp.MAX_TRACKED_SOURCES
    assert [s["client_ip"] for s in st["recent_sources"]] == ["10.0.0.1"]


# ---------------------------------------------------------------------------
# 「查看失败原因」：最近一条失败记录必须能从历史记录里被单独挑出来
# ---------------------------------------------------------------------------
#
# 背景：页面上的失败原因来自设备状态接口，而该接口过去只留"最新一条记录"。
# 失败 → 重试 → 成功是最常见的路径，成功记录一落库，失败原因当场消失 ——
# 于是"上次为什么失败"永远查不到，而这正是决定要不要再发、模板要不要改的依据。

def _cfg_row(device_id, status, message=None, rec_id=0, commands=None):
    """构造一条下发记录（只用到这几个字段，避免为纯函数测试搭 DB）。"""
    return SimpleNamespace(
        device_id=device_id, status=status, message=message,
        id=rec_id, commands=commands,
    )


def test_latest_failure_survives_later_success():
    """失败→重试成功之后，失败原因仍要能查到（不能被成功记录覆盖）。"""
    rows = [
        _cfg_row(7, cfg.STATE_APPLIED, rec_id=3),           # 最新一条：成功
        _cfg_row(7, cfg.STATE_FAILED, "回读没找到 NTP 服务器", rec_id=2),
        _cfg_row(7, cfg.STATE_FAILED, "更早的失败", rec_id=1),
    ]
    got = cfg.latest_failure_by_device(rows)
    assert got[7].message == "回读没找到 NTP 服务器", "应取最近的失败，而不是最旧的那条"


def test_latest_failure_ignores_unverified():
    """待确认不算失败。

    把它混进「失败原因」是危险的：unverified 的真实含义是"回读不可判定、
    配置很可能已经生效"，若在页面上标成失败，会诱导操作人重复下发生产设备。
    """
    rows = [_cfg_row(1, cfg.STATE_UNVERIFIED, "回读不可判定", rec_id=5)]
    assert cfg.latest_failure_by_device(rows) == {}


def test_latest_failure_is_per_device():
    rows = [
        _cfg_row(1, cfg.STATE_FAILED, "设备 1 的失败", rec_id=9),
        _cfg_row(2, cfg.STATE_APPLIED, rec_id=8),
        _cfg_row(1, cfg.STATE_APPLIED, rec_id=7),
    ]
    got = cfg.latest_failure_by_device(rows)
    assert set(got) == {1}
    assert got[1].message == "设备 1 的失败"


def test_latest_failure_handles_empty_and_none_device():
    assert cfg.latest_failure_by_device([]) == {}
    # device_id 为空的记录（设备已从纳管中移除）也不应抛错
    rows = [_cfg_row(None, cfg.STATE_FAILED, "x", rec_id=1)]
    assert cfg.latest_failure_by_device(rows)[None].message == "x"
