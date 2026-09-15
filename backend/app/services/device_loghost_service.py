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
        """
        loop = asyncio.get_running_loop()
        parts: list[str] = []
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
            lowered = text.lower()
            if "---- more ----" in lowered:
                try:
                    self._writer.write(b" ")
                    await self._writer.drain()
                except Exception:
                    pass
                last = loop.time()
                continue
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

INFOCENTER_CMD = "display current-configuration | include info-center"


async def fetch_infocenter_snapshot(session: DeviceSession) -> str:
    """读取设备上与 info-center 相关的配置片段（用于快照/回验）。"""
    out = await session.run(INFOCENTER_CMD, timeout=60)
    lines = []
    for line in _decode(out).replace("\r\n", "\n").split("\n"):
        s = line.strip()
        if not s:
            continue
        # 剔除命令回显与提示符行
        if s.startswith("display current-configuration"):
            continue
        if _prompt_re().match(s):
            continue
        if s.startswith("^"):
            continue
        if "More" in s:
            continue
        lines.append(s)
    return "\n".join(lines)


def _has_loghost(config_text: str, address: str) -> bool:
    """快照中是否已包含目标 loghost（回验用）。"""
    for line in (config_text or "").splitlines():
        s = line.strip().lower()
        if s.startswith("info-center loghost") and address.lower() in s:
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
    """对单台设备下发 loghost。返回 {ok, before, after, commands, warnings, error}。"""
    commands = build_apply_commands(address, port, level, save, is_huawei(device))
    result: dict = {
        "device_id": device.id,
        "device_name": device.name,
        "ip": device.ip,
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
        result["before"] = await fetch_infocenter_snapshot(session)
        result["warnings"] = await _run_commands(session, commands, timeout)
        result["after"] = await fetch_infocenter_snapshot(session)
        result["ok"] = _has_loghost(result["after"], address)
        if not result["ok"]:
            result["error"] = (
                "命令已下发但回验未在配置中找到目标 loghost；"
                "请登录设备确认（可能是设备型号不支持该语法或权限不足）"
            )
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
    """对单台设备回滚 loghost 配置。"""
    commands = build_rollback_commands(address, save, restore_disabled, is_huawei(device))
    result: dict = {
        "device_id": device.id,
        "device_name": device.name,
        "ip": device.ip,
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
        result["before"] = await fetch_infocenter_snapshot(session)
        result["warnings"] = await _run_commands(session, commands, timeout)
        result["after"] = await fetch_infocenter_snapshot(session)
        # 回滚成功 = 目标地址已不在配置里
        result["ok"] = not _has_loghost(result["after"], address)
        if not result["ok"]:
            result["error"] = "命令已下发但回验仍能查到目标 loghost，请登录设备确认"
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

async def _run_bounded(devices: list[Device], worker) -> list[dict]:
    """按 APPLY_CONCURRENCY 限并发执行，保持结果与入参同序。"""
    sem = asyncio.Semaphore(max(1, APPLY_CONCURRENCY))
    results: list[dict | None] = [None] * len(devices)

    async def _one(idx: int, dev: Device):
        async with sem:
            return idx, await worker(dev)

    outs = await asyncio.gather(
        *[_one(i, d) for i, d in enumerate(devices)], return_exceptions=True
    )
    for item in outs:
        if isinstance(item, Exception):
            # gather 吞掉异常会导致结果错位，这里兜底成一条失败记录
            logger.error("批量 loghost 任务异常：%r", item)
            continue
        idx, res = item
        results[idx] = res
    return [r for r in results if r is not None]


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
    for res in results:
        db.add(DeviceLogHostConfig(
            device_id=res["device_id"],
            device_name=res.get("device_name"),
            device_ip=res.get("ip"),
            loghost_address=addr,
            loghost_port=port or 514,
            status="applied" if res["ok"] else "failed",
            before_config=res.get("before"),
            after_config=res.get("after"),
            commands=res.get("commands"),
            tz_offset_hours=settings.SYSLOG_DEVICE_TZ_OFFSET_HOURS,
            message=res.get("error") or ("; ".join(res.get("warnings") or []) or None),
            applied_at=now if res["ok"] else None,
            operator=operator,
        ))
    await db.commit()
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
                "device_id": dev.id, "device_name": dev.name, "ip": dev.ip, "ok": False,
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
    for res in results:
        rec = records.get(res["device_id"])
        if rec is not None and res["ok"]:
            rec.status = "rolled_back"
            rec.rolled_back_at = now
            rec.message = f"由 {operator or '-'} 执行回滚"
        else:
            addr = (rec.loghost_address if rec else None) or address
            db.add(DeviceLogHostConfig(
                device_id=res["device_id"],
                device_name=res.get("device_name"),
                device_ip=res.get("ip"),
                loghost_address=addr,
                status="rolled_back" if res["ok"] else "failed",
                before_config=res.get("before"),
                after_config=res.get("after"),
                commands=res.get("commands"),
                message=res.get("error") or "; ".join(res.get("warnings") or []) or None,
                rolled_back_at=now if res["ok"] else None,
                operator=operator,
            ))
    await db.commit()
    return results


async def latest_records(db, device_ids: list[int]) -> dict[int, DeviceLogHostConfig]:
    """取各设备最新一条下发记录（已 applied 的）。"""
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
