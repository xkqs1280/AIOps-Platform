"""设备 CLI 交互终端服务 - 基于 asyncssh / telnetlib3 的 WebSocket 双向交互通道

提供网络设备（H3C Comware / 华为 VRP）的交互式命令行能力：
- SSH：asyncssh.create_process(term_type="xterm") 建立伪终端交互通道；
- Telnet：telnetlib3 shell 回调内自动登录 + 双向转发；
- 复用 backup_service 的老设备算法兼容配置（KEX/CIPHERS/HOSTKEYS/MACS），
  否则老设备 SSH 握手会失败；
- 输出按字节读取后智能解码（UTF-8 优先，GBK 兜底），解决中文乱码；
- 支持终端 resize（前端 xterm.js 尺寸变化时同步到远端 PTY）。
"""
import asyncio
import logging

import asyncssh
import telnetlib3

from app.config import settings
from app.services.backup_service import SSH_KEX, SSH_CIPHERS, SSH_HOSTKEYS, SSH_MACS

logger = logging.getLogger(__name__)

_TELNET_LOGIN_RE = (
    b"username:", b"login:", b"user name", b"password:", b"password"
)


def _decode_output(data) -> str:
    """把会话原始字节智能解码为文本（UTF-8 优先，GBK 兜底）。"""
    if isinstance(data, bytes):
        for enc in ("utf-8", "gbk"):
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, UnicodeError):
                continue
        return data.decode("gbk", errors="replace")
    return str(data)


class DeviceTerminal:
    """设备交互终端会话：管理 SSH/Telnet 连接与双向收发。"""

    def __init__(
        self,
        ip: str,
        username: str,
        password: str,
        protocol: str = "ssh",
        port: int | None = None,
        cols: int = 120,
        rows: int = 32,
    ):
        self.ip = ip
        self.username = username
        self.password = password
        self.protocol = protocol or "ssh"
        self.port = port
        self.cols = cols
        self.rows = rows
        self.conn = None          # asyncssh 连接
        self.process = None       # asyncssh SSHClientProcess
        self._reader = None
        self._writer = None
        self._logged_in = False
        self._closed = False
        self.on_output = None     # async callable(data: str)
        self.on_closed = None     # async callable(reason: str)

    async def connect(self, timeout: float = 20.0) -> None:
        """建立交互通道。失败抛异常由调用方处理。"""
        if self.protocol == "telnet":
            await self._connect_telnet(timeout)
        else:
            await self._connect_ssh(timeout)

    async def _connect_ssh(self, timeout: float) -> None:
        self.conn = await asyncio.wait_for(
            asyncssh.connect(
                self.ip,
                port=self.port or 22,
                username=self.username,
                password=self.password,
                known_hosts=settings.SSH_KNOWN_HOSTS or None,
                kex_algs=SSH_KEX,
                encryption_algs=SSH_CIPHERS,
                server_host_key_algs=SSH_HOSTKEYS,
                mac_algs=SSH_MACS,
                login_timeout=timeout,
                keepalive_interval=30,
                keepalive_count_max=3,
            ),
            timeout=timeout + 15,
        )
        # encoding=None：字节模式读取，避免 GBK 中文抛 ProtocolError
        self.process = await self.conn.create_process(
            term_type="xterm",
            term_size=(self.cols, self.rows),
            encoding=None,
        )

    async def _connect_telnet(self, timeout: float) -> None:
        reader, writer = await asyncio.wait_for(
            telnetlib3.open_connection(
                self.ip,
                port=self.port or 23,
                shell=self._telnet_shell,
                encoding=None,
                force_binary=True,
                connect_minwait=0.2,
            ),
            timeout=timeout + 5,
        )
        self._reader = reader
        self._writer = writer
        # 等待 shell 内完成登录（_telnet_shell 里置 _logged_in）
        deadline = asyncio.get_event_loop().time() + timeout
        while not self._logged_in and not self._closed:
            if asyncio.get_event_loop().time() > deadline:
                break
            await asyncio.sleep(0.3)
        if not self._logged_in and not self._closed:
            raise TimeoutError("Telnet 登录超时（未完成认证或未进入命令行）")

    async def _telnet_shell(self, reader, writer) -> None:
        """telnetlib3 shell 回调：自动登录 + 双向转发。"""
        self._reader = reader
        self._writer = writer
        buf = b""
        user_sent = False
        pass_sent = False
        while not self._closed:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=1.0)
            except asyncio.TimeoutError:
                chunk = b""
            except Exception:
                break
            if chunk:
                buf += chunk if isinstance(chunk, bytes) else chunk.encode("utf-8", "replace")
                # 先把原始输出转发给前端（登录提示也展示）
                if self.on_output:
                    try:
                        await self.on_output(_decode_output(chunk))
                    except Exception:
                        pass
            lower = buf.lower()
            if not user_sent and (b"username:" in lower or b"login:" in lower or b"user name" in lower):
                try:
                    writer.write((self.username + "\r\n").encode("utf-8", "replace"))
                    await writer.drain()
                except Exception:
                    break
                user_sent = True
                buf = b""
            elif user_sent and not pass_sent and (b"password:" in lower or b"password" in lower):
                try:
                    writer.write((self.password + "\r\n").encode("utf-8", "replace"))
                    await writer.drain()
                except Exception:
                    break
                pass_sent = True
                buf = b""
            elif pass_sent:
                self._logged_in = True
            if not chunk:
                await asyncio.sleep(0.05)
        # 连接结束，通知关闭
        if not self._closed:
            await self.close("Telnet 连接已断开")

    async def send(self, data: str) -> None:
        """发送用户输入到设备（data 为原始终端输入，含控制字符）。

        注意：SSH 通道以字节模式创建（encoding=None），stdin 是 bytes 流，
        必须显式编码，否则写入 str 会抛 TypeError 导致连接被关闭。
        """
        if self.protocol == "telnet":
            if self._writer:
                self._writer.write(data.encode("utf-8", "replace"))
                await self._writer.drain()
            return
        if self.process:
            self.process.stdin.write(data.encode("utf-8", "replace"))

    async def resize(self, cols: int, rows: int) -> None:
        """同步终端尺寸。change_terminal_size(width, height) 宽在前高在后。"""
        self.cols = cols or self.cols
        self.rows = rows or self.rows
        if self.protocol != "telnet" and self.process:
            try:
                self.process.change_terminal_size(self.cols, self.rows)
            except Exception:
                pass

    async def start_output_loop(self) -> None:
        """后台读取 SSH 设备输出并回调 on_output（Telnet 由 shell 回调驱动）。"""
        if self.protocol == "telnet":
            return
        while not self._closed:
            try:
                chunk = await self.process.stdout.read(4096)
                if not chunk:
                    break
                text = _decode_output(chunk)
                if self.on_output:
                    await self.on_output(text)
            except (asyncio.CancelledError, ConnectionError, asyncssh.Error):
                break
            except Exception as e:
                logger.warning(f"terminal read error {self.ip}: {e}")
                break
        if not self._closed:
            await self.close("连接已断开")

    async def close(self, reason: str = "已断开") -> None:
        """关闭会话。幂等。"""
        if self._closed:
            return
        self._closed = True
        try:
            if self.process:
                try:
                    self.process.terminate()
                except Exception:
                    pass
            if self.conn:
                try:
                    self.conn.close()
                except Exception:
                    pass
            if self._writer:
                try:
                    self._writer.close()
                except Exception:
                    pass
        except Exception:
            pass
        if self.on_closed:
            try:
                await self.on_closed(reason)
            except Exception:
                pass
