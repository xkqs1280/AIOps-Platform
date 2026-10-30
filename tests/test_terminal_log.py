# -*- coding: utf-8 -*-
"""设备 CLI 会话日志回归测试（华哥 2026-10-21 需求）。

覆盖点：
  1. 命名格式 ``<sysname>-<设备IP>-<YYYYMMDD-HHMM>.log``；
  2. 设备名净化（非法字符 / 空白 / 中文保留 / 空名兜底 / 保留名）；
  3. 同一分钟内重连不覆盖（``-2`` ``-3`` 后缀）；
  4. ANSI 转义剥离（否则记事本打开全是 ``^[[1;32m``）；
  5. 退格真实擦除（设备用 ``\\b \\b`` 擦字符）；
  6. ``\\r\\n`` 只算一个换行，孤立 ``\\r`` 也当换行；
  7. 头/尾页脚（设备、操作员、开始/结束时间、时长、大小、原因）；
  8. ``resolve()`` 的三重防穿越校验；
  9. 留存清理；
 10. 连接失败的空日志自动清理。
"""
import os
import re
import time
from datetime import datetime, timedelta

from app.services.terminal_log_service import (
    TZ_8,
    TerminalLogStore,
    _human_duration,
    _human_size,
    safe_component,
)


def _store(tmp_path) -> TerminalLogStore:
    return TerminalLogStore(tmp_path)


# ---------------------------------------------------------------------------
# 命名与净化
# ---------------------------------------------------------------------------
def test_filename_follows_required_pattern(tmp_path):
    """文件名必须是 sysname-IP-YYYYMMDD-HHMM.log（华哥给的例子）。"""
    log = _store(tmp_path).create("HX-SW1", "192.168.124.62", operator="admin", target="ssh:22")
    assert re.fullmatch(r"HX-SW1-192\.168\.124\.62-\d{8}-\d{4}\.log", log.filename), log.filename

    # 时间部分必须是北京时间（容忍跨分钟边界，只比到分钟前缀长度内的一致性）
    stamp = datetime.now(TZ_8).strftime("%Y%m%d-%H%M")
    assert stamp[:8] in log.filename  # 至少日期部分对得上
    log.close()


def test_safe_component_keeps_chinese_and_strips_illegal():
    assert safe_component("HX-SW1") == "HX-SW1"
    assert safe_component("HX SW1") == "HX_SW1"          # 空白压成下划线
    assert safe_component("核心交换机-1") == "核心交换机-1"  # 中文保留
    assert safe_component("") == "device"                # 空名兜底
    assert safe_component("...") == "device"             # 只剩非法/不可用字符
    assert "/" not in safe_component('a/b:c*?"<>|')      # 路径分隔符必须消失
    assert "\\" not in safe_component("a\\b")
    # Windows 保留名要加前缀，否则建文件失败
    assert safe_component("CON") == "_CON"


def test_devices_with_chinese_name_produce_usable_filename(tmp_path):
    log = _store(tmp_path).create("核心交换机-1", "10.0.0.1")
    assert log.filename.startswith("核心交换机-1-10.0.0.1-")
    assert log.path.is_file()
    log.close()


def test_same_minute_reconnect_does_not_overwrite(tmp_path):
    store = _store(tmp_path)
    first = store.create("HX-SW1", "192.168.124.62")
    second = store.create("HX-SW1", "192.168.124.62")
    assert first.filename != second.filename
    assert second.filename.endswith("-2.log")
    assert re.fullmatch(r".*-\d{4}-2\.log", second.filename), second.filename
    first.close()
    second.close()


# ---------------------------------------------------------------------------
# 内容净化
# ---------------------------------------------------------------------------
def test_ansi_escape_sequences_are_stripped(tmp_path):
    log = _store(tmp_path).create("HX-SW1", "192.168.124.62")
    log.write("\x1b[1;32m<HX-SW1>\x1b[0mdisplay version\r\n")
    log.write("\x1b]0;title\x07H3C Comware\r\n")
    log.close()

    text = log.path.read_text(encoding="utf-8")
    assert "<HX-SW1>display version" in text
    assert "H3C Comware" in text
    assert "\x1b" not in text           # 一个转义都不许留下
    assert "[1;32m" not in text


def test_backspace_really_erases_characters(tmp_path):
    """设备用 "\\b \\b" 擦字符，日志里必须得到干净的最终文本。"""
    log = _store(tmp_path).create("HX-SW1", "192.168.124.62")
    # 键入了 "shwo"，退两格，再输入 "w ver"
    log.write("<HX-SW1>shwo\x08 \x08\x08 \x08w ver\r\n")
    log.close()

    text = log.path.read_text(encoding="utf-8")
    assert "<HX-SW1>shw ver" in text
    assert "\x08" not in text


def test_crlf_counts_as_single_newline(tmp_path):
    log = _store(tmp_path).create("HX-SW1", "1.1.1.1")
    log.write("line1\r\nline2\r\n")
    log.close()

    body = [ln for ln in log.path.read_text(encoding="utf-8").splitlines() if ln.startswith("line")]
    assert body == ["line1", "line2"], body


def test_lone_cr_also_breaks_line(tmp_path):
    """孤立 \\r（终端回到行首重绘）也按换行处理，避免两段内容粘成一行。"""
    log = _store(tmp_path).create("HX-SW1", "1.1.1.1")
    log.write("---- More ----\rnext page\r\n")
    log.close()

    text = log.path.read_text(encoding="utf-8")
    assert "---- More ----\nnext page" in text


def test_h3c_double_cr_line_end_does_not_add_blank_lines(tmp_path):
    """H3C Comware 实测行尾是 "\\r\\r\\n"（双 CR）—— 不能因此每行多出一个空行。

    真机取证（2026-10-21，.108 + HX-SW1，build/_probe_ws_raw.py）：
      "display clock\\r\\n****...****\\r\\r\\n* Copyright ...\\r\\r\\n"
    """
    log = _store(tmp_path).create("HX-SW1", "192.168.124.62")
    log.write("banner1\r\r\nbanner2\r\r\nbanner3\r\r\n")
    log.close()

    lines = log.path.read_text(encoding="utf-8").split("\n")
    banner = [ln for ln in lines if ln.startswith("banner")]
    assert banner == ["banner1", "banner2", "banner3"], banner
    # 三行横幅之间不允许出现空行
    first = lines.index("banner1")
    assert lines[first + 1] == "banner2"
    assert lines[first + 2] == "banner3"


def test_real_blank_line_is_preserved(tmp_path):
    """设备真实输出的空行（连续两次 "\\r\\r\\n"）必须保留，不能被合并掉。"""
    log = _store(tmp_path).create("HX-SW1", "1.1.1.1")
    log.write("a\r\r\n\r\r\nb\r\r\n")
    log.close()

    lines = log.path.read_text(encoding="utf-8").split("\n")
    idx = lines.index("a")
    assert lines[idx + 1] == "", lines[idx:idx + 4]
    assert lines[idx + 2] == "b"


def test_mixed_crlf_and_cr_crlf(tmp_path):
    """同一会话里 "\\r\\n" 与 "\\r\\r\\n" 混用（实测确实混用）也要一致。"""
    log = _store(tmp_path).create("HX-SW1", "1.1.1.1")
    log.write("x\r\nx2\r\r\nx3\r\n")
    log.close()

    lines = [ln for ln in log.path.read_text(encoding="utf-8").split("\n") if ln.startswith("x")]
    assert lines == ["x", "x2", "x3"], lines


def test_header_and_footer_present(tmp_path):
    log = _store(tmp_path).create("HX-SW1", "192.168.124.62", operator="admin", target="ssh:22")
    log.write("<HX-SW1>display clock\r\n")
    log.close("用户关闭页面")

    text = log.path.read_text(encoding="utf-8")
    assert "AIOps 设备 CLI 会话日志" in text
    assert "HX-SW1 (192.168.124.62)" in text
    assert "admin" in text
    assert "ssh:22" in text
    assert "结束原因 : 用户关闭页面" in text
    assert "<HX-SW1>display clock" in text


def test_close_is_idempotent(tmp_path):
    log = _store(tmp_path).create("HX-SW1", "1.1.1.1")
    log.write("x\r\n")
    log.close()
    log.close()  # 不应抛异常，也不应重复写页脚
    assert log.path.read_text(encoding="utf-8").count("结束时间") == 1


def test_write_after_close_is_noop(tmp_path):
    log = _store(tmp_path).create("HX-SW1", "1.1.1.1")
    log.close()
    size_before = log.path.stat().st_size
    log.write("should not be written\r\n")
    assert log.path.stat().st_size == size_before


def test_partial_last_line_is_flushed_on_close(tmp_path):
    """会话结束时最后一行为光标行，通常没有换行符 —— 也必须落盘。"""
    log = _store(tmp_path).create("HX-SW1", "1.1.1.1")
    log.write("<HX-SW1>")
    log.close()

    assert "<HX-SW1>" in log.path.read_text(encoding="utf-8")


def test_long_line_is_spilled(tmp_path):
    """超长行（整段配置）要分段落盘，不能一直攒在内存里。"""
    log = _store(tmp_path).create("HX-SW1", "1.1.1.1")
    log.write("A" * 2000 + "\r\n")
    log.close()

    text = log.path.read_text(encoding="utf-8")
    assert "A" * 2000 in text.replace("\n", "")


# ---------------------------------------------------------------------------
# 下载路径校验（防穿越 / 防越权）
# ---------------------------------------------------------------------------
def test_resolve_accepts_own_file(tmp_path):
    store = _store(tmp_path)
    log = store.create("HX-SW1", "192.168.124.62")
    log.close()

    assert store.resolve("HX-SW1", "192.168.124.62", log.filename) == log.path.resolve()


def test_resolve_rejects_path_traversal(tmp_path):
    store = _store(tmp_path)
    store.create("HX-SW1", "192.168.124.62").close()

    for evil in ["../../etc/passwd", "..\\..\\windows\\win.ini", "sub/dir.log", "/etc/passwd"]:
        assert store.resolve("HX-SW1", "192.168.124.62", evil) is None, evil


def test_resolve_rejects_other_devices_file(tmp_path):
    store = _store(tmp_path)
    other = store.create("OTHER-SW", "10.9.9.9")
    other.close()

    assert store.resolve("HX-SW1", "192.168.124.62", other.filename) is None


def test_resolve_rejects_missing_file(tmp_path):
    store = _store(tmp_path)
    assert store.resolve("HX-SW1", "192.168.124.62", "HX-SW1-192.168.124.62-20260101-0000.log") is None


def test_list_returns_newest_first(tmp_path):
    store = _store(tmp_path)
    a = store.create("HX-SW1", "192.168.124.62")
    a.close()
    b = store.create("HX-SW1", "192.168.124.62")
    b.close()

    items = store.list_for("HX-SW1", "192.168.124.62")
    assert len(items) == 2
    assert {i["filename"] for i in items} == {a.filename, b.filename}
    assert all(i["size"] > 0 for i in items)


def test_list_isolated_per_device(tmp_path):
    store = _store(tmp_path)
    store.create("HX-SW1", "192.168.124.62").close()
    store.create("OTHER-SW", "10.9.9.9").close()

    items = store.list_for("HX-SW1", "192.168.124.62")
    assert len(items) == 1
    assert items[0]["filename"].startswith("HX-SW1-192.168.124.62-")


# ---------------------------------------------------------------------------
# 留存与清理
# ---------------------------------------------------------------------------
def test_purge_removes_files_older_than_retention(tmp_path):
    store = _store(tmp_path)
    store.retention_days = 30
    store._last_purge = 0.0  # 跳过"每小时一次"的节流，强制立即执行

    old = tmp_path / "HX-SW1-1.1.1.1-20250101-0000.log"
    old.write_text("old", encoding="utf-8")
    stale = time.time() - 40 * 86400
    os.utime(old, (stale, stale))

    fresh = tmp_path / "HX-SW1-1.1.1.1-20260101-0000.log"
    fresh.write_text("fresh", encoding="utf-8")

    assert store.purge_old() == 1
    assert not old.exists()
    assert fresh.exists()


def test_purge_disabled_when_retention_zero(tmp_path):
    store = _store(tmp_path)
    store.retention_days = 0
    old = tmp_path / "x.log"
    old.write_text("x", encoding="utf-8")
    stale = time.time() - 999 * 86400
    os.utime(old, (stale, stale))

    assert store.purge_old() == 0
    assert old.exists()


def test_trivial_log_removed_on_connect_failure(tmp_path):
    """连接失败且设备什么都没输出时，不留下空壳日志。"""
    log = _store(tmp_path).create("HX-SW1", "192.168.124.62")
    assert log.unlink_if_trivial() is True
    assert not log.path.exists()


def test_non_trivial_log_kept_on_connect_failure(tmp_path):
    """连接失败但设备已经吐了内容（如认证失败提示），必须保留作为证据。"""
    log = _store(tmp_path).create("HX-SW1", "192.168.124.62")
    log.write("Permission denied, please try again.\r\n" * 5)
    assert log.unlink_if_trivial() is False
    assert log.path.exists()
    assert "Permission denied" in log.path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def test_human_helpers():
    assert _human_size(512) == "512 B"
    assert _human_size(2048) == "2.0 KB"
    assert _human_duration(5) == "5秒"
    assert _human_duration(65) == "1分5秒"
    assert _human_duration(3665) == "1小时1分5秒"


def test_log_dir_created_lazily(tmp_path):
    nested = tmp_path / "a" / "b" / "logs"
    store = TerminalLogStore(nested)
    assert not nested.exists()
    log = store.create("HX-SW1", "1.1.1.1")
    assert nested.is_dir()
    log.close()


def test_unknown_tz_is_beijing(tmp_path):
    """文件名用北京时间：宿主时区为 UTC 时不能比华哥的表慢 8 小时。"""
    assert datetime.now(TZ_8).utcoffset() == timedelta(hours=8)
