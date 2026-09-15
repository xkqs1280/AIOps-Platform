# -*- coding: utf-8 -*-
"""设备 CLI 终端服务回归测试：退格键归一（DEL 0x7F -> BS 0x08）。

背景（真机实测，探针见 build/remote_probe/probe_backspace.py、
probe_raw_telnet.py、probe_telnet_proxy.py）：

  - xterm.js 的 Backspace 在 Keyboard.ts 的 `case 8` 里硬编码发送 DEL(0x7F)；
  - 华为 AR 走 telnet（经 telnetlib3 协商 IAC DO ECHO / WILL SGA / WILL TTYPE
    之后）时，设备把 0x7F 当作**不可识别的控制字符**：原样回显 BEL(0x07)
    且不删除字符，前端表现为「退格键删不掉字符」；而同一台设备在**纯 NVT**
    （裸 socket、不接受任何协商）下 0x7F 又完全正常 —— 说明是协商状态导致的差异；
  - BS(0x08) 在 SSH / Telnet × 华为 / H3C 四种组合下实测均正确执行退格。

因此 send() 统一把 0x7F 归一为 0x08：前端不必改动，两个协议、两个厂商都正确。
"""
from app.services.terminal_service import DeviceTerminal


class _FakeWriter:
    """模拟 telnetlib3 的 TelnetWriter。"""

    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf += data

    async def drain(self):
        pass


class _FakeStdin:
    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf += data


class _FakeProcess:
    def __init__(self):
        self.stdin = _FakeStdin()


def _mk(protocol: str) -> DeviceTerminal:
    return DeviceTerminal(ip="127.0.0.1", username="u", password="p", protocol=protocol)


async def test_telnet_backspace_del_is_normalized_to_bs():
    """telnet 通道（华为 AR 中招场景）：DEL 必须被换成 BS。"""
    term = _mk("telnet")
    writer = _FakeWriter()
    term._writer = writer
    await term.send("dis\x7f")
    assert bytes(writer.buf) == b"dis\x08"


async def test_ssh_backspace_del_is_normalized_to_bs():
    """ssh 通道同样归一（0x08 在华为/H3C 的 SSH 下实测也正确）。"""
    term = _mk("ssh")
    term.process = _FakeProcess()
    await term.send("display cloc" + "\x7f" * 4 + "version\r\n")
    assert bytes(term.process.stdin.buf) == b"display cloc" + b"\x08" * 4 + b"version\r\n"


async def test_existing_bs_is_left_untouched():
    """已经是 BS 的输入不得被二次改写。"""
    term = _mk("telnet")
    writer = _FakeWriter()
    term._writer = writer
    await term.send("\x08\x08")
    assert bytes(writer.buf) == b"\x08\x08"


async def test_plain_input_untouched():
    term = _mk("ssh")
    term.process = _FakeProcess()
    await term.send("display version\r\n")
    assert bytes(term.process.stdin.buf) == b"display version\r\n"


async def test_other_control_sequences_untouched():
    """ESC 序列（方向键）等其它控制字符不得被改动。"""
    term = _mk("ssh")
    term.process = _FakeProcess()
    await term.send("\x1b[A\x1b[B\r")
    assert bytes(term.process.stdin.buf) == b"\x1b[A\x1b[B\r"
