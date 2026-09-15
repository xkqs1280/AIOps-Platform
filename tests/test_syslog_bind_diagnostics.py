# -*- coding: utf-8 -*-
"""syslog UDP 接收器「绑定失败」自诊断的回归测试。

背景（生产实测）：Windows 生产服务器报
``syslog 接收器绑定 0.0.0.0:514 失败 OSError: [WinError 10048]``，
而页面上的排查建议却是 Linux 的 ``CAP_NET_BIND_SERVICE`` —— 平台在 Windows 上
根本不用特权端口，这条提示把排查方向带偏了。

因此约定：
- 排查建议**按平台生成**，Windows 分支不得出现 CAP_NET_BIND_SERVICE；
- Windows 上要能把占用端口的进程（PID + 映像名）直接查出来报给前端；
- 诊断逻辑必须是纯函数可测（不真的起 netstat / 真机）。
"""
import os
import sys
from types import SimpleNamespace

from app.services import syslog_receiver as sr


# ---------------------------------------------------------------------------
# 1. netstat 输出解析
# ---------------------------------------------------------------------------

NETSTAT_SAMPLE = """
活动连接

  协议  本地地址          外部地址        状态           PID
  UDP    0.0.0.0:500           *:*                                    1234
  UDP    0.0.0.0:514           *:*                                    4321
  UDP    127.0.0.1:514         *:*                                    4321
  UDP    [::]:514              *:*                                    8899
  UDP    0.0.0.0:5514          *:*                                    7777
  TCP    0.0.0.0:514           0.0.0.0:0    LISTENING               5555
"""


def test_parse_udp_occupiers_picks_matching_port_only():
    pids = sr.parse_udp_occupiers(NETSTAT_SAMPLE, 514)
    assert pids == [4321, 8899]  # 去重、按出现顺序、IPv6 也算


def test_parse_udp_occupiers_ignores_tcp_lines():
    # TCP 行即使端口相同也不算（UDP 的绑定失败与 TCP 无关）
    assert 5555 not in sr.parse_udp_occupiers(NETSTAT_SAMPLE, 514)


def test_parse_udp_occupiers_empty_and_garbage():
    assert sr.parse_udp_occupiers("", 514) == []
    assert sr.parse_udp_occupiers("随便一段中文\n二进制\x00乱码", 514) == []


# ---------------------------------------------------------------------------
# 2. tasklist 输出解析
# ---------------------------------------------------------------------------

def test_parse_task_image_from_csv():
    line = '"kiwi.exe","4321","Console","1","12,345 K"\n'
    assert sr.parse_task_image(line) == "kiwi.exe"


def test_parse_task_image_none_when_no_task():
    # 中文 Windows 上 tasklist 查不到时输出的是提示语，不是 CSV
    assert sr.parse_task_image("信息: 没有运行的任务匹配指定标准。") is None
    assert sr.parse_task_image("") is None


# ---------------------------------------------------------------------------
# 3. 平台分支的排查建议
# ---------------------------------------------------------------------------

def _win_err(winerror: int) -> BaseException:
    """构造与 Windows `socket.bind` 抛出的等价的 OSError。

    本机实测：Windows 上 UDP 端口冲突的真实异常是
    ``OSError winerror=10048 errno=10048``（errno 并不映射成 POSIX 的 100，
    两者同值），``str(e)`` 为 ``[WinError 10048] 通常每个套接字地址…``。
    这里照实测构造；代码分支同时判 winerror 与 errno，两种写法都能命中。
    """
    exc = OSError(winerror, "通常每个套接字地址(协议/网络地址/端口)只允许使用一次。")
    exc.winerror = winerror  # type: ignore[attr-defined]
    return exc


def test_windows_hints_never_mention_linux_capability():
    """核心回归：Windows 的**任意**分支都不得再出现 CAP_NET_BIND_SERVICE。

    生产上就是这条提示把人带偏的（Windows 根本没有 <1024 特权端口的概念）。
    覆盖 10048/10013/10049 及其它未知错误码，防止以后有人把 Linux 文案改回来。
    """
    for winerror in (10048, 10013, 10049, 10060, 12345):
        joined = " ".join(sr._bind_hints(_win_err(winerror), os_name="nt"))
        assert "CAP_NET_BIND_SERVICE" not in joined, winerror
        assert "AmbientCapabilities" not in joined, winerror
    # 已占用分支还应给出 Windows 上真正可执行的命令
    occupied = " ".join(sr._bind_hints(_win_err(10048), os_name="nt"))
    assert "netstat" in occupied
    assert "10048" in occupied


def test_windows_hints_cover_access_denied_and_reserved_range():
    hints = sr._bind_hints(_win_err(10013), os_name="nt")
    assert any("excludedportrange" in h for h in hints)


def test_windows_hints_flag_bad_host_address():
    hints = sr._bind_hints(_win_err(10049), os_name="nt")
    assert any("10049" in h for h in hints)


def test_linux_hints_keep_capability_advice():
    exc = OSError(98, "Address already in use")
    exc.errno = 98  # type: ignore[attr-defined]
    hints = sr._bind_hints(exc, os_name="posix")
    joined = " ".join(hints)
    assert "CAP_NET_BIND_SERVICE" in joined
    assert "ss -lnup" in joined


def test_all_hints_mention_port_change_escape_hatch():
    for os_name in ("nt", "posix"):
        hints = sr._bind_hints(OSError(98, "in use"), os_name=os_name)
        assert any("SYSLOG_UDP_PORT" in h for h in hints)


# ---------------------------------------------------------------------------
# 4. 占用者识别（含"是否本平台自身进程"）
# ---------------------------------------------------------------------------

def test_occupier_reported_when_third_party():
    hints = sr._bind_hints(_win_err(10048), ["PID 4321 (kiwi.exe)"], os_name="nt")
    assert any("kiwi.exe" in h for h in hints)
    assert not any("本平台自身进程" in h for h in hints)


def test_occupier_recognised_as_own_process():
    own = os.path.basename(sys.executable)  # 测试进程即"本平台自身"
    hints = sr._bind_hints(_win_err(10048), [f"PID 1234 ({own})"], os_name="nt")
    assert any("本平台自身进程" in h for h in hints)


def test_udp_port_occupiers_noop_on_non_windows():
    # 确保在 Linux 上不会去调 netstat（也就不会拖慢/污染失败路径）
    if os.name == "nt":
        assert isinstance(sr.udp_port_occupiers(514), list)
    else:
        assert sr.udp_port_occupiers(514) == []


def test_run_quiet_survives_non_utf8_output():
    """回归：子进程输出非 UTF-8 字节时，_run_quiet 必须仍返回内容而不是空串。

    中文 Windows 上 netstat / tasklist 吐的就是 cp936 字节；若用默认 text=True，
    UnicodeDecodeError 会在**读管道线程**里抛出，主流程只拿到空 stdout，
    于是"端口已被占用却查不出占用者"（本机实测踩过）。这里用 GBK 字节复现。
    """
    out = sr._run_quiet(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xbb\\xd8\\xd3\\xc3')"]
    )
    assert out.strip() != ""


# ---------------------------------------------------------------------------
# 5. 状态接口透出
# ---------------------------------------------------------------------------

def test_receiver_status_exposes_bind_diagnostics():
    st = sr.receiver_status()
    assert "bind_hints" in st
    assert "bind_occupier" in st
    # 未发生绑定失败时为空，前端会回退到通用提示
    assert st["bind_hints"] is None
    assert st["bind_occupier"] is None


def test_reset_state_clears_bind_diagnostics():
    sr._stats["bind_hints"] = ["x"]
    sr._stats["bind_occupier"] = ["PID 1 (a.exe)"]
    sr.reset_state()
    st = sr.receiver_status()
    assert st["bind_hints"] is None
    assert st["bind_occupier"] is None


def test_simple_namespace_smoke():
    """占位说明：诊断函数只依赖 winerror/errno 两个属性，任何异常对象都能用。"""
    assert sr._bind_hints(SimpleNamespace(winerror=10048), os_name="nt")
