"""Telnet 客户端服务 - 基于 telnetlib3 的异步 Telnet 连接

解决：backup_service / h3c_inspection_service / compliance_service 此前对
protocol="telnet" 只是把端口改成 23，仍走 asyncssh（SSH 协议）→ telnet 设备
必然握手失败。本模块提供真正的 Telnet 会话：登录、执行命令、读取完整输出。

关键点：
- telnetlib3 的 reader/writer 是 asyncio 流，需在事件循环中读写；
- 登录后先关闭分页（screen-length），再执行命令；
- 通过设备提示符（prompt）判定命令输出结束，兼容 Huawei VRP / H3C Comware；
- 连接/登录/命令均设超时，避免挂死。
"""
import asyncio
import logging
import re

import telnetlib3

logger = logging.getLogger(__name__)

# 常见提示符：<AR1> / <H3C-SW> / AR1# / H3C# 等
PROMPT_RE = re.compile(r"^(<[^>]*>|\S+#)\s*$")
# 分页提示
MORE_RE = re.compile(r"---- More ----", re.I)


def _decode_bytes(data: bytes) -> str:
    """把原始字节智能解码为正确中文。

    设备配置常含中文，H3C 传统编码为 GBK/GB2312，新设备或英文模式为 UTF-8。
    UTF-8 优先，GBK 兜底，保证任何情况下都不抛 UnicodeDecodeError。
    """
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("gbk", errors="replace")


async def _drain(reader, timeout: float = 2.0) -> str:
    """读取并丢弃当前可读数据（吸收 banner / 登录提示等）。"""
    out = []
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=1.0)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        out.append(chunk)
    return _decode_bytes(b"".join(out))


async def _capture_prompt(writer, reader, timeout: float = 8.0) -> str | None:
    """发送回车，返回最后一行非空内容作为设备提示符。"""
    try:
        writer.write(b"\r\n")
        await writer.drain()
    except Exception:
        pass
    raw = await _drain(reader, timeout)
    lines = [ln for ln in raw.replace("\r\n", "\n").split("\n") if ln.strip()]
    if not lines:
        return None
    return lines[-1].strip()


async def _login(writer, reader, username: str, password: str, timeout: float = 20.0) -> None:
    """等待登录提示并输入账号密码。

    telnetlib3 默认发送 IAC 协商（回显关闭等），设备通常出现：
        Username:  /  login:  /  Password:
    这里轮询读取直到出现提示，再写入凭据。
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    buf = b""
    user_sent = False
    pass_sent = False
    while loop.time() < deadline:
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=1.0)
        except asyncio.TimeoutError:
            chunk = b""
        if chunk:
            buf += chunk
        lower = buf.lower()
        if not user_sent and (b"username:" in lower or b"login:" in lower or b"user name" in lower):
            writer.write((username + "\r\n").encode("utf-8", errors="replace"))
            await writer.drain()
            user_sent = True
            buf = b""
        elif user_sent and not pass_sent and (b"password:" in lower or b"password" in lower):
            writer.write((password + "\r\n").encode("utf-8", errors="replace"))
            await writer.drain()
            pass_sent = True
            buf = b""
        elif pass_sent:
            # 登录完成后等待提示符出现
            lines = [ln for ln in _decode_bytes(buf).replace("\r\n", "\n").split("\n") if ln.strip()]
            if lines and PROMPT_RE.match(lines[-1].strip()):
                return
        if not chunk:
            await asyncio.sleep(0.2)
    raise TimeoutError("Telnet 登录超时（未完成认证或未进入命令行）")


async def run_command(
    ip: str,
    username: str,
    password: str,
    command: str,
    port: int = 23,
    timeout: int = 60,
    login_timeout: float = 25.0,
) -> str:
    """通过 Telnet 连接设备并执行单条命令，返回完整输出。

    - 登录后尝试关闭分页（screen-length disable / screen-length 0 temporary）；
    - 发送命令后读取，直到设备提示符再次出现；遇分页提示自动发空格续传；
    - 返回原始输出文本（含命令回显，由调用方按需清理）。
    """
    # 编码处理：H3C 设备配置常含中文（如 description 中文），编码可能是
    # GBK/GB2312 或 UTF-8（取决于设备版本/语言模式）。若固定按 UTF-8 解码
    # 会抛 UnicodeDecodeError 导致备份失败。
    # 方案：encoding=False 以字节模式读取（telnetlib3 二进制 reader，永不抛
    # 解码错），拿到完整输出后用 _decode_bytes 自动识别 UTF-8/GBK 还原中文。
    reader, writer = await telnetlib3.open_connection(
        ip, port=port, connect_minwait=0.05, connect_maxwait=0.5,
        encoding=False,
    )
    try:
        # 登录
        await asyncio.wait_for(
            _login(writer, reader, username, password, login_timeout),
            timeout=login_timeout + 5,
        )

        # 关闭分页（两种厂商命令都试一次）
        for cmd in ("screen-length 0 temporary", "screen-length disable"):
            try:
                writer.write((cmd + "\r\n").encode("utf-8", errors="replace"))
                await writer.drain()
                await _drain(reader, timeout=1.5)
            except Exception:
                pass

        # 捕获稳定提示符
        prompt = await _capture_prompt(writer, reader, timeout=6)
        if not prompt:
            prompt = await _capture_prompt(writer, reader, timeout=6)

        # 发送目标命令
        writer.write((command + "\r\n").encode("utf-8", errors="replace"))
        await writer.drain()

        # 读取命令输出直到提示符再次出现
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        last_data = loop.time()
        raw_parts: list[bytes] = []
        saw_return = False
        while loop.time() < deadline:
            try:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=2)
            except asyncio.TimeoutError:
                # 无数据停顿：仅当已读到配置结尾 return 才判定结束（配置已完整输出）；
                # 否则继续等待，避免大配置输出中途停顿被提前截断。
                if raw_parts and saw_return and (loop.time() - last_data) > 4:
                    break
                continue
            if not chunk:
                break
            last_data = loop.time()
            raw_parts.append(chunk)
            text = _decode_bytes(chunk)
            if MORE_RE.search(text):
                try:
                    writer.write(b" ")
                    await writer.drain()
                except Exception:
                    pass
                last_data = loop.time()
            # 识别配置结尾 return（H3C/华为配置固定以 return 结尾）
            for ln in text.replace("\r\n", "\n").split("\n"):
                if ln.strip() == "return":
                    saw_return = True
                    break
            if prompt:
                for ln in text.replace("\r\n", "\n").split("\n"):
                    if ln.strip() == prompt:
                        return _clean_raw_output("".join(_decode_bytes(b) for b in raw_parts), prompt)
        return _clean_raw_output("".join(_decode_bytes(b) for b in raw_parts), prompt)
    finally:
        try:
            writer.close()
        except Exception:
            pass


def _smart_decode(raw: str) -> str:
    """把 latin-1 字节保真的字符串还原为正确编码的中文。

    优先按 UTF-8 解码（现代设备/纯 ASCII），失败则按 GBK 解码（H3C 传统中文），
    仍失败则 errors=replace 兜底，保证任何情况都不抛异常。
    """
    try:
        data = raw.encode("latin-1")
    except Exception:
        return raw
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("gbk", errors="replace")


def _clean_raw_output(raw: str, prompt: str | None) -> str:
    """清理 telnet 原始输出：
    - 先把 latin-1 字节流智能解码为正确编码（UTF-8/GBK）中文；
    - 去掉 NUL (\x00) 填充字节（设备常见，会让 endswith('return') 判断失效）；
    - 去掉结尾的设备提示符行（<AC> / H3C# 等）；
    - 保留 \r\n 换行（兼容配置文本解析）。
    """
    text = _smart_decode(raw)
    text = text.replace("\x00", "").replace("\r\n", "\n")
    lines = text.split("\n")
    while lines and lines[-1].strip() == "":
        lines.pop()
    if lines and prompt and lines[-1].strip() == prompt:
        lines.pop()
    # 去掉命令回显行（display current-configuration 所在行及其之前）
    for i, line in enumerate(lines):
        if "display current-configuration" in line:
            lines = lines[i + 1:]
            break
    return "\n".join(lines).strip()


async def fetch_full_config(
    ip: str,
    username: str,
    password: str,
    port: int = 23,
    timeout: int = 300,
) -> str:
    """Telnet 采集设备完整运行配置（display current-configuration）。"""
    return await run_command(
        ip, username, password, "display current-configuration",
        port=port, timeout=timeout,
    )
