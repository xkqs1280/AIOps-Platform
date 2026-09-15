"""设备侧「远程日志主机（loghost）」批量下发与回滚。

这是整个设备日志中心里**唯一会真实改动设备配置**的部分，因此按生产变更的高标准来做：

  1. **预览**：先生成命令清单返回给调用方确认（默认不执行）；
  2. **先快照**：连接后先取 ``display current-configuration | include info-center`` 原状；
  3. **再下发**：逐条下发并**逐条检查错误标记**（Unrecognized / Incomplete / Permission denied …）；
  4. **后回验**：再次读取配置，确认 loghost 已生效；
  5. **可回滚**：回滚用 ``undo info-center loghost <addr>``，并按快照决定是否把
     ``undo info-center enable`` 一并还原；每条记录都落库（谁、何时、改了什么）。
  6. **支持不保存**（``save=False``）：只做运行时下发，重启即失效，用于灰度验证。

安全：``address`` / ``level`` / ``port`` 都会被拼进设备命令行，故**必须严格校验**
（IP 用 ``ipaddress`` 解析、级别限枚举、端口限 1~65535），杜绝命令注入。

设备访问复用 ``backup_service`` 已验证的 SSH 算法白名单 —— 新版 paramiko/asyncssh 用
默认算法连不上老 H3C 设备（握手报 no acceptable host key），必须一并放宽。
"""
import asyncio
import ipaddress
import logging
import re
import socket
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import settings
from app.models.device import Device
from app.models.device_log import DeviceLogHostConfig
from app.services.backup_service import (
    SSH_CIPHERS,
    SSH_HOSTKEYS,
    SSH_KEX,
    SSH_MACS,
    _capture_prompt,
    _decode,
    _drain,
    PAGING_DISABLE_CMDS,
)
from app.services.credential_service import reveal_secret

logger = logging.getLogger(__name__)

# 允许的日志级别（H3C info-center level 关键字）
ALLOWED_LEVELS = (
    "emergencies", "alerts", "critical", "errors",
    "warnings", "notifications", "informational", "debugging",
)

# 单设备命令执行超时（秒）
CONFIG_TIMEOUT = 120
# 普通命令的「空转多久算输出结束」阈值
COMMAND_IDLE = 2.0
# 保存命令的空闲阈值要放宽：写盘期间设备（华为尤甚）可能连续数秒无输出，
# 沿用 2 秒会提前结束读取——既漏掉 "Save the configuration successfully."
# 导致成功判定失真，又把残余输出留给下一条命令，污染快照。
SAVE_IDLE = 8.0
# 批量下发的并发上限：配置变更是高风险操作，并发放小便于观察与人工介入
APPLY_CONCURRENCY = 4
# 单次请求允许的设备数上限
MAX_DEVICES_PER_REQUEST = 100

# 设备返回的错误标记（H3C Comware / 华为 VRP）
_ERROR_MARKERS = (
    "% Unrecognized command",
    "% Incomplete command",
    "% Permission denied",
    "% Wrong parameter",
    "% Too many parameters",
    "% Ambiguous command",
    "Error:",
    "Invalid input",
)
# 需要自动答 Y 的确认提示
_CONFIRM_RE = None  # 延迟编译（re 在下方 import，保持模块头部依赖整洁）

# 终端控制序列：部分设备即使在 vt100 下也会吐出颜色/光标控制码，混在行里会让
# 「地址子串匹配」这种朴素判断失效 → 匹配前统一剥掉。
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]")

# 分页提示标记。各厂商/型号写法不一（H3C/华为 `---- More ----`、部分型号
# `--More--`、`<--- More --->`），只认一种写法的话，读到第一屏就会因「空闲」而
# 提前结束 —— 目标行落在后面几页里就永远看不到，表现为「配置明明生效却回验不到」。
_PAGING_MARKERS = ("---- more ----", "--more--", "<--- more --->")

# 回读配置的命令候选：先窄后宽。
#   - 窄命令靠 `| include <关键词>` 过滤，输出小、快；
#   - 但**并非所有型号都支持该过滤语法**，有的会直接报错、有的静默返回空 ——
#     后者无法与「设备上确实没有该配置」区分，因此只在**命令明确报错**时退化，
#     避免对「本来就没配 info-center」的设备白白整段 dump。
SNAPSHOT_CMDS = (
    "display current-configuration | include info-center",
    "display current-configuration | include loghost",
    "display current-configuration",
)
SNAPSHOT_TIMEOUT = 60

# 快照里保留哪些行：这份快照的用途就是「info-center 相关配置」，
# 因此即便退化成整段 dump，也只留可能承载日志主机配置的行（控制体积）。
_SNAPSHOT_KEEP_RE = re.compile(r"info-center|loghost|logging\s+host", re.IGNORECASE)

# 判定「配置里存在目标日志主机」的行首关键字。保持严格：必须是日志主机配置行且
# 含**完整 IP**，避免把正文里偶现的地址当成配置（曾把华为 OID 的前四段误认成 IP）。
_LOGHOST_LINE_RE = re.compile(r"^(?:info-center\s+loghost|logging\s+host|loghost)\b", re.IGNORECASE)


def _confirm_re():
    global _CONFIRM_RE
    if _CONFIRM_RE is None:
        import re
        _CONFIRM_RE = re.compile(r"\[Y/N\]|\(y/n\)|Are you sure|continue\?", re.IGNORECASE)
    return _CONFIRM_RE


PROMPT_RE = None


def _prompt_re():
    global PROMPT_RE
    if PROMPT_RE is None:
        import re
        PROMPT_RE = re.compile(r"^(<[^>]*>|\[[^\[\]]*\]|\S+#)\s*$")
    return PROMPT_RE


# ---------------------------------------------------------------------------
# 参数校验（防命令注入）
# ---------------------------------------------------------------------------

def validate_address(address: str) -> str:
    """校验并归一化日志主机地址（必须为合法 IP）。"""
    try:
        return str(ipaddress.ip_address(str(address).strip()))
    except ValueError as e:
        raise ValueError(f"日志主机地址不是合法 IP：{address!r}") from e


def validate_level(level: str) -> str:
    lv = str(level or "informational").strip().lower()
    if lv not in ALLOWED_LEVELS:
        raise ValueError(
            f"日志级别不合法：{level!r}，允许值：{', '.join(ALLOWED_LEVELS)}"
        )
    return lv


def validate_port(port) -> int | None:
    """校验日志主机端口。``None`` / 空串 表示"用默认端口"；其余必须是 1~65535。

    注意这里用 ``is None or == ""`` 而不是 ``if not port``：端口 **0 不是"未设置"，
    而是非法值**——静默当作默认 514 会把调用方的错误参数吞掉，下发到设备上却成了
    另一回事，事后极难排查。
    """
    if port is None or (isinstance(port, str) and not port.strip()):
        return None
    try:
        p = int(port)
    except (TypeError, ValueError) as e:
        raise ValueError(f"日志主机端口不是整数：{port!r}") from e
    if not 1 <= p <= 65535:
        raise ValueError(f"端口超出范围（1~65535）：{p}")
    return p


# ---------------------------------------------------------------------------
# 厂商差异：保存配置
# ---------------------------------------------------------------------------

# 华为 VRP 与 H3C Comware 的 info-center 命令一致，但「保存配置」语法不同：
#   H3C : `save force` —— force 表示不再交互确认
#   华为: `save`       —— **没有 force 参数**；force 会被当成文件名而报错
#        实测：Error: Invalid file name or Invalid extension ( *.cfg, *.zip ).
#        实测机型/版本：S5700-28C-HI(V200R001C00)、AR(VRP V500R011)
# 华为主机执行 `save` 会弹 "Are you sure to continue?[Y/N]"，由会话自动应答（见
# DeviceSession._read_until_idle）。该交互在华为上会先输出一行
# "Error: Please choose 'YES' or 'NO' first before pressing 'Enter'." —— 实测与
# 应答方式（Y / Y\r\n / Y\n）无关，且随后**一定**会打印 "Save the configuration
# successfully."，属固有噪音，故在 errors_in 中按行甄别（见 _BENIGN_LINE_MARKERS）。
_HUAWEI_HINTS = ("华为", "huawei", "vrp")

# 华为确认交互的良性噪音行 + 对应的成功标志（两者同时出现才判定为良性）
_BENIGN_LINE_MARKERS = ("Please choose 'YES' or 'NO'",)
_SAVE_OK_MARKERS = ("Save the configuration successfully",)


def is_huawei(device) -> bool:
    """是否为华为 VRP 设备（决定保存命令用 `save` 还是 `save force`）。

    只看 ``vendor`` / ``model`` 两处：厂商字段可能填"华为"也可能填 "Huawei"；
    而 AR 路由器的型号常写成 "AR (VRP V500R011)"，所以一并匹配 "vrp"。
    """
    blob = " ".join(
        str(getattr(device, f, "") or "") for f in ("vendor", "model")
    ).lower()
    return any(h in blob for h in _HUAWEI_HINTS)


def save_command(huawei: bool) -> str:
    """按厂商给出保存配置的命令。"""
    return "save" if huawei else "save force"


def source_command(level: str, huawei: bool) -> str:
    """设置「输出到日志主机的模块与级别」的命令（两家语法不同）。

    H3C : ``info-center source default loghost level informational``
    华为: ``info-center source default channel loghost log level informational``

    实测：把 H3C 写法直接发给华为会报
        Error: Unrecognized command found at '^' position.   （^ 指向 loghost）
    华为在 ``default`` 之后必须接 ``channel`` 指定通道名，且 level 前要说明
    log/trap/debug 哪一类（这里是日志，故为 ``log level``）。
    实测机型：S5700-28C-HI(V200R001C00)。
    """
    if huawei:
        return f"info-center source default channel loghost log level {level}"
    return f"info-center source default loghost level {level}"


# ---------------------------------------------------------------------------
# 命令生成
# ---------------------------------------------------------------------------

def build_apply_commands(address: str, port: int | None = None,
                         level: str = "informational", save: bool = True,
                         huawei: bool = False) -> list[str]:
    """生成下发 loghost 的命令序列（H3C Comware / 华为 VRP 通用语法）。

    ``huawei=True`` 时末条保存命令用 `save`（华为没有 `save force`，见上文）。
    """
    addr = validate_address(address)
    lv = validate_level(level)
    p = validate_port(port)
    loghost_cmd = f"info-center loghost {addr}"
    if p and p != 514:
        loghost_cmd += f" port {p}"
    cmds = [
        "system-view",
        "info-center enable",
        loghost_cmd,
        source_command(lv, huawei),
        "return",
    ]
    if save:
        cmds.append(save_command(huawei))
    return cmds


def build_rollback_commands(address: str, save: bool = True,
                            restore_disabled: bool = False,
                            huawei: bool = False) -> list[str]:
    """生成回滚命令序列。``restore_disabled`` 为真时一并恢复 info-center 关闭状态。"""
    addr = validate_address(address)
    cmds = [
        "system-view",
        f"undo info-center loghost {addr}",
    ]
    if restore_disabled:
        cmds.append("undo info-center enable")
    cmds.append("return")
    if save:
        cmds.append(save_command(huawei))
    return cmds


def infocenter_was_disabled(config_text: str | None) -> bool:
    """从配置快照判断下发前 info-center 是否处于关闭状态。"""
    if not config_text:
        return False
    for line in config_text.splitlines():
        if line.strip().lower() == "undo info-center enable":
            return True
    return False


# ---------------------------------------------------------------------------
# 会话（SSH / Telnet 统一接口）
# ---------------------------------------------------------------------------

class DeviceSession:
    """统一的设备命令会话：SSH 与 Telnet 的读写接口一致。

    提供 ``run(cmd)`` 执行单条命令并按「空闲即结束」判定输出结束 —— 不依赖具体提示符，
    因为进入 system-view 后提示符会从 ``<SW>`` 变为 ``[SW]``，用固定提示符判定会卡住。
    """

    def __init__(self, writer, reader, conn=None, closer=None):
        self._writer = writer
        self._reader = reader
        self._conn = conn
        self._closer = closer
        self.transcript: list[tuple[str, str]] = []

    async def _read_until_idle(self, idle: float, deadline: float,
                               until: tuple[str, ...] | None = None) -> str:
        """读到「空闲 idle 秒」或「出现 until 中任一标志」为止。

        ``until`` 供长耗时命令使用（保存配置）：写盘期间设备可能长时间静默，
        单靠空闲阈值猜结束时机并不可靠 —— 并发下发时实测会漏读成功标志，
        于是输出里只剩确认交互的噪音行，被误判成报错。

        分页：命中分页标记就补一个空格翻页。标记可能被 TCP 分片切成两块
        （``---- Mo`` + ``re ----``），只看当前块会漏判 → 用「上一块尾部 + 本块」
        组成窗口来匹配；窗口只保留极短尾部，避免旧标记被反复命中而无限翻页。
        """
        loop = asyncio.get_running_loop()
        parts: list[str] = []
        prev_tail = ""
        last = loop.time()
        while loop.time() < deadline:
            try:
                chunk = await asyncio.wait_for(self._reader.read(65536), timeout=1.0)
            except asyncio.TimeoutError:
                if parts and (loop.time() - last) >= idle:
                    break
                # 尚未收到任何输出：继续等到 deadline
                continue
            if not chunk:
                break
            last = loop.time()
            text = _decode(chunk)
            parts.append(text)
            window = (prev_tail + text).lower()
            if any(m in window for m in _PAGING_MARKERS):
                try:
                    self._writer.write(b" ")
                    await self._writer.drain()
                except Exception:
                    pass
                last = loop.time()
                prev_tail = ""
                continue
            prev_tail = text[-32:]
            m = _confirm_re().search(text)
            if m:
                try:
                    self._writer.write(b"Y\r\n")
                    await self._writer.drain()
                except Exception:
                    pass
                last = loop.time()
            if until and any(u.lower() in "".join(parts).lower() for u in until):
                break
        return "".join(parts)

    async def run(self, command: str, timeout: int = CONFIG_TIMEOUT,
                  idle: float = 2.0, until: tuple[str, ...] | None = None) -> str:
        """执行一条命令并返回输出（含命令回显）。

        ``until`` 给出即可在该标志出现时立刻结束读取（见 _read_until_idle）。
        """
        loop = asyncio.get_running_loop()
        try:
            self._writer.write((command + "\r\n").encode("utf-8", errors="replace"))
            await self._writer.drain()
        except Exception as e:
            raise ConnectionError(f"写入设备失败：{type(e).__name__}: {e}") from e
        out = await self._read_until_idle(idle, loop.time() + timeout, until=until)
        self.transcript.append((command, out))
        return out

    def errors_in(self, output: str) -> list[str]:
        """输出中命中的错误标记（同一标记去重）。

        逐行判定而非整段匹配：华为 `save` 的确认交互会先打一行
        "Error: Please choose 'YES' or 'NO' first before pressing 'Enter'."
        随后仍会成功保存（实测与应答方式 Y / Y\\r\\n / Y\\n 无关，属固有噪音）。
        因此**仅当同一输出里同时出现保存成功标志**时才忽略该行，
        既消掉噪音又不掩盖真正的报错。
        """
        text = output or ""
        low = text.lower()
        save_ok = any(m.lower() in low for m in _SAVE_OK_MARKERS)
        hits: list[str] = []
        for line in text.splitlines():
            line_low = line.lower()
            if save_ok and any(b.lower() in line_low for b in _BENIGN_LINE_MARKERS):
                continue
            for m in _ERROR_MARKERS:
                if m.lower() in line_low:
                    if m not in hits:
                        hits.append(m)
                    break
        return hits

    async def close(self) -> None:
        for fn in (self._closer, getattr(self._writer, "close", None)):
            try:
                if fn:
                    fn()
                    break
            except Exception:
                pass
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass


async def open_device_session(device: Device, timeout: int = CONFIG_TIMEOUT) -> DeviceSession:
    """按设备的 ``mgmt_protocol`` 打开会话（SSH / Telnet）。"""
    user = device.mgmt_username or settings.DEFAULT_DEVICE_USERNAME
    pwd = reveal_secret(device.mgmt_password) or settings.DEFAULT_DEVICE_PASSWORD
    if not user or not pwd:
        raise ValueError("设备未配置管理账号或密码")

    protocol = (device.mgmt_protocol or "ssh").lower()
    port = device.mgmt_port or (23 if protocol == "telnet" else 22)

    if protocol == "telnet":
        import telnetlib3
        from app.services.telnet_client import _login

        reader, writer = await telnetlib3.open_connection(
            device.ip, port=port, connect_minwait=0.05, connect_maxwait=0.5, encoding=False,
        )
        await asyncio.wait_for(_login(writer, reader, user, pwd, 25.0), timeout=30)
        session = DeviceSession(writer, reader)
        # 关闭分页，避免配置输出被截断
        for cmd in PAGING_DISABLE_CMDS:
            try:
                await session.run(cmd, timeout=10, idle=1.0)
            except Exception:
                pass
        return session

    import asyncssh

    if settings.SSH_STRICT_HOST_KEY_CHECKING and not settings.SSH_KNOWN_HOSTS:
        raise ValueError("启用了 SSH 主机密钥校验，但未配置 SSH_KNOWN_HOSTS")
    conn = await asyncio.wait_for(
        asyncssh.connect(
            device.ip, port=port, username=user, password=pwd,
            known_hosts=settings.SSH_KNOWN_HOSTS or None,
            kex_algs=SSH_KEX, encryption_algs=SSH_CIPHERS,
            server_host_key_algs=SSH_HOSTKEYS, mac_algs=SSH_MACS,
            login_timeout=30, keepalive_interval=30, keepalive_count_max=3,
        ),
        timeout=timeout + 15,
    )
    # encoding=None：字节模式读取，避免设备输出 GBK 中文抛 ProtocolError
    writer, reader, _ = await conn.open_session(
        term_type="vt100", term_size=(200, 50), encoding=None,
    )
    session = DeviceSession(writer, reader, conn=conn)
    await _capture_prompt(writer, reader, timeout=10)
    for cmd in PAGING_DISABLE_CMDS:
        try:
            await session.run(cmd, timeout=10, idle=1.0)
        except Exception:
            pass
    return session


# ---------------------------------------------------------------------------
# 单设备操作
# ---------------------------------------------------------------------------

INFOCENTER_CMD = SNAPSHOT_CMDS[0]


def _strip_ansi(text: str) -> str:
    """剥掉终端控制序列（颜色/光标控制）。"""
    return _ANSI_RE.sub("", text or "")


def _strip_leading_prompt(line: str) -> str:
    """去掉行首提示符（``<SW>`` / ``[SW]`` / ``SW#``），便于识别命令回显行。"""
    return re.sub(r"^(?:<[^>]*>|\[[^\[\]]*\]|\S+#)\s*", "", (line or "").strip())


def _snapshot_lines(raw) -> str:
    """把 `display current-configuration` 的原始回包整理成配置行文本。

    三处坑，任一踩中都会让后面的行首匹配失配，表现成「命令明明下发成功、回验却说
    找不到」：
      1. **行分隔不一定是 ``\\r\\n``**：设备可能只发 ``\\r``。原实现
         ``replace("\\r\\n", "\\n").split("\\n")`` 遇到裸 CR 会把整段粘成一行，
         而同一文件里 ``errors_in`` 用的是 ``splitlines()``（认 CR）—— 两处口径
         不一致，于是「报错检测正常、快照匹配全灭」；
      2. **控制序列**混在行内，地址被切开；
      3. 退化成整段 dump 时会把几千行都塞进快照字段。
    """
    text = _decode(raw)
    # 仅 CR 也是合法行分隔（老设备 telnet/串口常见），必须先归一
    text = _strip_ansi(text).replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        # 剔除命令回显（含带提示符的 `<SW>display ...`）、错误指示符（^ 指向出错位置）
        if _strip_leading_prompt(s).lower().startswith("display "):
            continue
        if s.startswith("%") or s.startswith("^"):
            continue
        if _prompt_re().match(s):
            continue
        if any(m in s.lower() for m in _PAGING_MARKERS):
            continue
        # 只保留可能承载日志主机配置的行（见 _SNAPSHOT_KEEP_RE）
        if not _SNAPSHOT_KEEP_RE.search(s):
            continue
        lines.append(s)
    return "\n".join(lines)


def _summarize(text: str, limit: int = 3) -> str:
    """回读内容的短摘要，用于把「到底读到了什么」写进消息里。"""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return "（回读为空）"
    head = " / ".join(lines[:limit])
    if len(lines) > limit:
        head += f" …（共 {len(lines)} 行）"
    return head[:300]


class Snapshot:
    """一次配置回读的结果，以及「这份回读是否可信」的判定依据。

    ``trusted`` 是回验语义的基石：**空回读一律不可信** —— 无法区分「设备上真没有」
    与「该型号不支持这条过滤语法」，把它当成「没配」就会把已生效的下发误判成失败。
    """

    __slots__ = ("text", "command", "trusted", "notes")

    def __init__(self, text: str, command: str, trusted: bool, notes: list[str] | None = None):
        self.text = text
        self.command = command
        self.trusted = trusted
        self.notes = list(notes or [])

    def __repr__(self) -> str:  # pragma: no cover - 仅调试可读性
        return (f"Snapshot(command={self.command!r}, trusted={self.trusted}, "
                f"lines={len(self.text.splitlines())}, notes={self.notes!r})")


async def fetch_infocenter_snapshot(session: DeviceSession) -> Snapshot:
    """回读设备上与 info-center / 日志主机相关的配置。

    返回 :class:`Snapshot` —— 除了文本，还带上「用了哪条命令」「是否可信」。
    **可信 = 命令没报错且确实读到了内容**；空回读一律不可信，因为无法区分
    「设备上真没有」与「该型号不支持这条过滤语法」。把不可信当成「没配」，
    就会把已经生效的下发误判成失败。
    """
    notes: list[str] = []
    text = ""
    for cmd in SNAPSHOT_CMDS:
        try:
            out = await session.run(cmd, timeout=SNAPSHOT_TIMEOUT)
        except Exception as e:
            notes.append(f"`{cmd}` 读取失败：{type(e).__name__}: {e}")
            continue
        errs = session.errors_in(out)
        got = _snapshot_lines(out)
        if errs:
            notes.append(f"`{cmd}` 报错：{'; '.join(errs)}")
            if not text:
                text = got
            continue
        if got:
            return Snapshot(got, cmd, True, notes)
        notes.append(f"`{cmd}` 无输出")
        if not text:
            text = got
    return Snapshot(text, SNAPSHOT_CMDS[-1], False, notes)


def _has_loghost(config_text: str, address: str) -> bool:
    """快照中是否已包含目标日志主机（回验用）。

    与 ``_decode`` 同一行分隔口径，避免「传输用 CR、匹配按 LF」互相打架。
    """
    addr = (address or "").lower()
    for line in _strip_ansi(config_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = line.strip()
        if not addr or addr not in s.lower():
            continue
        if _LOGHOST_LINE_RE.match(s):
            return True
    return False


async def _run_commands(session: DeviceSession, commands: list[str],
                        timeout: int) -> list[str]:
    """逐条下发并收集错误标记（返回 warnings 文案）。

    保存命令单独放宽空闲阈值（见 SAVE_IDLE）：设备写盘期间长时间静默，
    用普通阈值会让读取提前结束并留下残余输出。同时对保存命令以
    ``_SAVE_OK_MARKERS`` 作为结束标志 —— 出现成功提示即收工，不必干等；
    若保存真的失败（没有成功提示），仍会按空闲阈值正常收尾并如实报错。
    """
    warnings: list[str] = []
    for cmd in commands:
        if cmd.startswith("save"):
            out = await session.run(cmd, timeout=timeout, idle=SAVE_IDLE,
                                    until=_SAVE_OK_MARKERS)
        else:
            out = await session.run(cmd, timeout=timeout, idle=COMMAND_IDLE)
        errs = session.errors_in(out)
        if errs:
            warnings.append(f"`{cmd}` → {'; '.join(errs)}")
    return warnings


async def apply_to_device(
    device: Device,
    address: str,
    port: int | None = None,
    level: str = "informational",
    save: bool = True,
    timeout: int = CONFIG_TIMEOUT,
) -> dict:
    """对单台设备下发 loghost。

    返回 ``{state, ok, before, after, commands, warnings, error}``。
    ``state`` 是回验三态，见 :func:`_judge_apply`；``ok`` 仅为「已确认生效」。
    """
    commands = build_apply_commands(address, port, level, save, is_huawei(device))
    result: dict = {
        "device_id": device.id,
        "device_name": device.name,
        "ip": device.ip,
        "state": "failed",
        "ok": False,
        "before": None,
        "after": None,
        "commands": "\n".join(commands),
        "warnings": [],
        "error": None,
    }
    session = None
    try:
        session = await open_device_session(device, timeout)
        result["before"] = (await fetch_infocenter_snapshot(session)).text
        result["warnings"] = await _run_commands(session, commands, timeout)
        after = await fetch_infocenter_snapshot(session)
        result["after"] = after.text
        result["state"], result["error"] = _judge_apply(address, after, result["warnings"])
        result["ok"] = result["state"] == "applied"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        logger.warning("设备 %s(%s) 下发 loghost 失败：%s", device.name, device.ip, e)
    finally:
        if session is not None:
            await session.close()
    return result


async def rollback_device(
    device: Device,
    address: str,
    save: bool = True,
    restore_disabled: bool = False,
    timeout: int = CONFIG_TIMEOUT,
) -> dict:
    """对单台设备回滚 loghost 配置（回验同样是三态）。"""
    commands = build_rollback_commands(address, save, restore_disabled, is_huawei(device))
    result: dict = {
        "device_id": device.id,
        "device_name": device.name,
        "ip": device.ip,
        "state": "failed",
        "ok": False,
        "before": None,
        "after": None,
        "commands": "\n".join(commands),
        "warnings": [],
        "error": None,
    }
    session = None
    try:
        session = await open_device_session(device, timeout)
        result["before"] = (await fetch_infocenter_snapshot(session)).text
        result["warnings"] = await _run_commands(session, commands, timeout)
        after = await fetch_infocenter_snapshot(session)
        result["after"] = after.text
        result["state"], result["error"] = _judge_rollback(address, after, result["warnings"])
        result["ok"] = result["state"] == "rolled_back"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        logger.warning("设备 %s(%s) 回滚 loghost 失败：%s", device.name, device.ip, e)
    finally:
        if session is not None:
            await session.close()
    return result


# ---------------------------------------------------------------------------
# 批量编排 + 落库
# ---------------------------------------------------------------------------

# 回验三态。用「三态」而不是「成功/失败」是现场教训：某型号（HX-7506X）上
# `display current-configuration | include info-center` 回读为空，而配置其实已经
# 下发成功 —— 二态模型下这必然被报成「失败」，操作人很可能据此重复下发，
# 对生产设备就是二次真实变更。**「读不到」不等于「没配」**，必须单独成一个状态。
STATE_APPLIED = "applied"          # 已确认生效
STATE_ROLLED_BACK = "rolled_back"  # 已确认回滚
STATE_UNVERIFIED = "unverified"    # 命令已下发且无报错，但回读不可信，无法确认
STATE_FAILED = "failed"            # 确认失败（命令报错，或回读可信但没有/仍有目标）


def _readback_diag(after: Snapshot) -> str:
    """把「回读用了什么命令、读到了什么」压成一句，便于远程定位。"""
    lines = len(after.text.splitlines())
    return f"（回读：`{after.command}`，{lines} 行：{_summarize(after.text)}）"


def _judge_apply(address: str, after: Snapshot, command_errors: list[str]) -> tuple[str, str | None]:
    """下发后的回验判定 → (state, message)。"""
    if _has_loghost(after.text, address):
        return STATE_APPLIED, None
    diag = _readback_diag(after)
    if command_errors:
        return STATE_FAILED, f"下发命令报错，配置很可能未生效：{'; '.join(command_errors)}{diag}"
    if after.trusted:
        return STATE_FAILED, (
            f"命令已下发且无报错，但回读到的配置里没有目标日志主机 {address}{diag}"
        )
    why = "；".join(after.notes) or "回读不到配置"
    return STATE_UNVERIFIED, (
        f"命令已下发且无报错，但回验不可判定（{why}）{diag}。"
        "该型号可能不支持 `display current-configuration | include ...` 过滤语法，"
        f"或回读方式与设备语法不匹配。请登录设备执行 "
        f"`display current-configuration | include {address}` 人工确认后再决定是否重发"
    )


def _judge_rollback(address: str, after: Snapshot, command_errors: list[str]) -> tuple[str, str | None]:
    """回滚后的回验判定 → (state, message)。"""
    still_there = _has_loghost(after.text, address)
    diag = _readback_diag(after)
    if command_errors:
        return STATE_FAILED, f"回滚命令报错：{'; '.join(command_errors)}{diag}"
    if not after.trusted:
        why = "；".join(after.notes) or "回读不到配置"
        return STATE_UNVERIFIED, (
            f"回滚命令已下发且无报错，但回验不可判定（{why}）{diag}。"
            "请登录设备确认目标日志主机是否已移除"
        )
    if still_there:
        return STATE_FAILED, f"命令已下发但回读仍能查到目标日志主机 {address}{diag}"
    return STATE_ROLLED_BACK, None

def _device_failure(device: Device, message: str) -> dict:
    """构造一条「本台未完成」的结果。

    字段必须与 ``apply_to_device`` / ``rollback_device`` 的返回结构完全一致：
    调用方（落库、前端渲染）都按这些键取值，少一个键就会在收尾阶段炸成 500。
    """
    return {
        "device_id": getattr(device, "id", None),
        "device_name": getattr(device, "name", None),
        "ip": getattr(device, "ip", None),
        "state": STATE_FAILED,
        "ok": False, "before": None, "after": None, "commands": None,
        "warnings": [], "error": message,
    }


async def _run_bounded(devices: list[Device], worker) -> list[dict]:
    """按 APPLY_CONCURRENCY 限并发执行，保持结果与入参同序。

    **每台设备必须恰好产出一条结果**。这里不能用「捕获 Exception 后 continue」
    的写法：一是 ``asyncio.CancelledError`` 自 3.8 起继承 ``BaseException``，
    既不会被 ``except Exception`` 接住，也不会被下面的 ``isinstance(item, Exception)``
    认出来；二是 gather 用 ``return_exceptions=True`` 收进来的异常对象并不能
    解包成 ``(idx, res)``。

    生产事故（70 台设备批量下发/回滚 → 前端只看到
    "Request failed with status code 500"、且 FastAPI 对未捕获异常只回纯文本
    ``Internal Server Error``，连 detail 都没有）就是这条链路：某台设备的
    读操作在内部被取消 → ``CancelledError`` 逃出 ``apply_to_device`` 的
    ``except Exception`` → 被 gather 收进结果列表 → 解包报 TypeError → 500。
    设备配置其实已经改了一半，操作人却以为整批失败。
    """
    sem = asyncio.Semaphore(max(1, APPLY_CONCURRENCY))
    results: list[dict | None] = [None] * len(devices)

    async def _one(idx: int, dev: Device):
        async with sem:
            try:
                return idx, await worker(dev)
            except asyncio.CancelledError:
                # 只有「本任务/本请求自己被取消」（关停、客户端断开）才继续向上传播；
                # 单台设备会话内部冒出来的取消，按本台失败处理，不能连累整批。
                task = asyncio.current_task()
                if task is not None and getattr(task, "cancelling", lambda: 0)():
                    raise
                logger.error("设备 %s(%s) 会话被中断（CancelledError），本台未完成",
                             getattr(dev, "name", "?"), getattr(dev, "ip", "?"))
                return idx, _device_failure(dev, "设备会话被中断（CancelledError），本台未完成")
            except BaseException as e:  # noqa: BLE001 — 兜底：绝不让单台炸掉整批
                logger.exception("设备 %s(%s) 批处理异常", getattr(dev, "name", "?"),
                                 getattr(dev, "ip", "?"))
                return idx, _device_failure(dev, f"{type(e).__name__}: {e}")

    outs = await asyncio.gather(
        *[_one(i, d) for i, d in enumerate(devices)], return_exceptions=True
    )
    for item in outs:
        if isinstance(item, BaseException):
            # 走到这里说明连上面的兜底都没接住（例如整批被取消），
            # 记日志跳过即可——**绝不能去解包**，否则又是一个 500。
            logger.error("批量 loghost 任务异常：%r", item)
            continue
        try:
            idx, res = item
        except (TypeError, ValueError):
            logger.error("批量 loghost 结果结构异常：%r", item)
            continue
        results[idx] = res
    return [r for r in results if r is not None]


async def _safe_rollback(db) -> None:
    try:
        await db.rollback()
    except Exception:
        pass


def _mark_persist_failure(results: list[dict], exc: Exception, action: str) -> None:
    """留痕落库失败时，把真相写到每台设备的结果里，而不是让请求变成 500。

    设备配置**已经改过了**，此时回 500 只会让操作人以为没生效而重复下发
    （对生产设备是二次真实变更，风险更高）。所以如实返回逐台结果，并把
    「记录没落库」这件事显式挂到 warnings 上。
    """
    note = (f"{action}记录落库失败（{type(exc).__name__}: {exc}）；"
            "设备配置已变更，请勿重复操作，并联系维护人员核查数据库")
    for res in results:
        res.setdefault("warnings", []).append(note)


async def apply_loghost_to_devices(
    db, devices: list[Device], address: str, port: int | None = None,
    level: str = "informational", save: bool = True,
    operator: str | None = None, timeout: int = CONFIG_TIMEOUT,
) -> list[dict]:
    """批量下发并逐台落库记录（成功与失败都记，便于审计与重试）。"""
    addr = validate_address(address)
    port = validate_port(port)
    level = validate_level(level)
    if len(devices) > MAX_DEVICES_PER_REQUEST:
        raise ValueError(f"单次最多下发 {MAX_DEVICES_PER_REQUEST} 台设备")

    results = await _run_bounded(
        devices,
        lambda d: apply_to_device(d, addr, port, level, save, timeout),
    )

    now = datetime.now(timezone.utc)
    try:
        for res in results:
            state = res.get("state") or (STATE_APPLIED if res["ok"] else STATE_FAILED)
            db.add(DeviceLogHostConfig(
                device_id=res["device_id"],
                device_name=res.get("device_name"),
                device_ip=res.get("ip"),
                loghost_address=addr,
                loghost_port=port or 514,
                status=state,
                before_config=res.get("before"),
                after_config=res.get("after"),
                commands=res.get("commands"),
                tz_offset_hours=settings.SYSLOG_DEVICE_TZ_OFFSET_HOURS,
                message=res.get("error") or ("; ".join(res.get("warnings") or []) or None),
                # 只有「已确认生效」才记 applied_at；待确认（unverified）不记，
                # 否则状态表里会出现「有时间戳却待确认」的自相矛盾
                applied_at=now if state == STATE_APPLIED else None,
                operator=operator,
            ))
        await db.commit()
    except Exception as e:  # noqa: BLE001 — 见 _mark_persist_failure
        logger.exception("loghost 下发记录落库失败（设备配置已变更）")
        await _safe_rollback(db)
        _mark_persist_failure(results, e, "下发")
    return results


async def rollback_loghost_on_devices(
    db, devices: list[Device], address: str | None = None, save: bool = True,
    operator: str | None = None, timeout: int = CONFIG_TIMEOUT,
    records: dict[int, DeviceLogHostConfig] | None = None,
) -> list[dict]:
    """批量回滚。``records`` 提供各设备的下发记录（用于取地址与还原 info-center 状态）。"""
    if len(devices) > MAX_DEVICES_PER_REQUEST:
        raise ValueError(f"单次最多回滚 {MAX_DEVICES_PER_REQUEST} 台设备")
    records = records or {}

    async def _worker(dev: Device) -> dict:
        rec = records.get(dev.id)
        addr = (rec.loghost_address if rec else None) or address
        if not addr:
            return {
                "device_id": dev.id, "device_name": dev.name, "ip": dev.ip,
                "state": STATE_FAILED, "ok": False,
                "error": "未提供日志主机地址，且该设备没有下发记录，无法回滚",
                "before": None, "after": None, "commands": None, "warnings": [],
            }
        return await rollback_device(
            dev, addr, save=save,
            restore_disabled=infocenter_was_disabled(rec.before_config if rec else None),
            timeout=timeout,
        )

    results = await _run_bounded(devices, _worker)

    now = datetime.now(timezone.utc)
    try:
        for res in results:
            state = res.get("state") or (STATE_ROLLED_BACK if res["ok"] else STATE_FAILED)
            rec = records.get(res["device_id"])
            if rec is not None and state == STATE_ROLLED_BACK:
                rec.status = STATE_ROLLED_BACK
                rec.rolled_back_at = now
                rec.message = f"由 {operator or '-'} 执行回滚"
            else:
                addr = (rec.loghost_address if rec else None) or address
                db.add(DeviceLogHostConfig(
                    device_id=res["device_id"],
                    device_name=res.get("device_name"),
                    device_ip=res.get("ip"),
                    loghost_address=addr,
                    status=state,
                    before_config=res.get("before"),
                    after_config=res.get("after"),
                    commands=res.get("commands"),
                    message=res.get("error") or "; ".join(res.get("warnings") or []) or None,
                    rolled_back_at=now if state == STATE_ROLLED_BACK else None,
                    operator=operator,
                ))
        await db.commit()
    except Exception as e:  # noqa: BLE001 — 见 _mark_persist_failure
        logger.exception("loghost 回滚记录落库失败（设备配置已变更）")
        await _safe_rollback(db)
        _mark_persist_failure(results, e, "回滚")
    return results


async def latest_records(db, device_ids: list[int]) -> dict[int, DeviceLogHostConfig]:
    """取各设备最新一条下发记录。

    不按状态过滤：`unverified`（待确认）同样可能已经把 loghost 配到设备上了，
    回滚时正需要它的地址与 before 快照 —— 过滤掉会让「待确认」的设备无法回滚。
    """
    if not device_ids:
        return {}
    rows = (await db.execute(
        select(DeviceLogHostConfig)
        .where(DeviceLogHostConfig.device_id.in_(device_ids))
        .order_by(DeviceLogHostConfig.id.desc())
    )).scalars().all()
    out: dict[int, DeviceLogHostConfig] = {}
    for r in rows:
        if r.device_id not in out:
            out[r.device_id] = r
    return out


# ---------------------------------------------------------------------------
# 平台侧地址候选
# ---------------------------------------------------------------------------

def local_address_candidates() -> list[str]:
    """本机可被设备访问的候选地址（供前端下拉选择，不做自动决定）。

    多网卡主机（如测试服 ens33/ens37）自动选错网卡会让设备连不上，
    因此这里只给候选，由使用者确认。
    """
    cands: list[str] = []
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in cands and not ip.startswith("127."):
                cands.append(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and ip not in cands and not ip.startswith("127."):
                cands.insert(0, ip)
        finally:
            s.close()
    except Exception:
        pass
    if settings.SYSLOG_ADVERTISE_ADDRESS and settings.SYSLOG_ADVERTISE_ADDRESS not in cands:
        cands.insert(0, settings.SYSLOG_ADVERTISE_ADDRESS)
    return cands
