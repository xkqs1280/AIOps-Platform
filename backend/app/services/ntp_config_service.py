"""NTP 配置下发服务：模板构建 → 快照 → 下发 → 回验三态 → 回滚。

整体流程与 ``device_loghost_service`` 完全同构（那是同类需求的成熟实现，这里是
"设备侧配置变更"这一族的第二个成员，刻意保持结构一致，避免两套心智模型）。

但**快照的读法完全不同**，这是本模块最需要注意的地方：

  - loghost 看的是 ``display info-center``（运行时状态）；
  - NTP 在 H3C 上看的是 ``display current-configuration | include ntp`` 与
    ``display ntp-service status``。前者是配置形态（空回读不可信），
    后者是运行时状态（空回读也可信，因为"没配就是没配"）。
  - 回验的**唯一判据**是「回读里能不能找到目标 NTP 服务器地址」。

模板（华哥定的默认 H3C 版）：

    system-view
    clock protocol ntp
    ntp-service enable
    ntp-service unicast-server <addr>
    quit
    save force

两点刻意的处理：

  1. **不带 `ntp-service update-calendar enable`** —— 华哥明确要求去掉。
     该命令让 NTP 同时校准设备硬件日历，在部分型号上会引入额外写操作；
     授时的目标是"设备显示时间准"，系统时钟同步即可。

  2. **华为走 `ntp-service` 还是 `ntp-service unicast-server` 需要分支**。
     华为 VRP 的等价命令是 `ntp-service unicast-server`（V5/V8 一致），
     但**没有 `clock protocol ntp` 这条**（VRP 用 `ntp-service enable` 即可，
     时钟协议由 `ntp-service refclock-master` 等控制）。本模块按厂商分支构建。
"""
import asyncio
import ipaddress
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import settings
from app.models.device import Device
from app.models.ntp import NtpCommandTemplate, NtpDeviceConfig
from app.services.device_loghost_service import (
    COMMAND_IDLE,
    CONFIG_TIMEOUT,
    MAX_DEVICES_PER_REQUEST,
    SAVE_IDLE,
    STATE_APPLIED,
    STATE_FAILED,
    STATE_ROLLED_BACK,
    STATE_UNVERIFIED,
    DeviceSession,
    _SAVE_OK_MARKERS,
    _run_bounded,
    _safe_rollback,
    _summarize,
    is_huawei,
    open_device_session,
)

logger = logging.getLogger(__name__)

# 回读命令（按可信度由近到远尝试）
#   - 配置形态：`display current-configuration | include ntp`（H3C / 华为通用语法）
#   - 状态形态：`display ntp-service status`（H3C）/ `display ntp status`（华为 V8）
SNAPSHOT_CMDS = (
    "display current-configuration | include ntp",
    "display current-configuration | include clock protocol",
    "display ntp-service status",
    "display ntp status",
)

SNAPSHOT_TIMEOUT = 60

# 状态类命令（空输出也可信：运行时真值，没有就是没有）
_STATE_CMDS = ("display ntp-service status", "display ntp status")

# 错误标记（设备对不合语法/不支持命令的典型回显）
_ERROR_MARKERS = (
    "% Unrecognized command",
    "% Wrong parameter",
    "% Incomplete command",
    "% Too many parameters",
    "% Ambiguous command",
    "Error:",
    "Unrecognized command found",
    "% Invalid input",
)


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------

def validate_address(address: str) -> str:
    """校验 NTP 服务器地址（IP 或主机名），防命令注入。

    NTP 服务器地址会被**拼进要下发到设备的命令行**里。如果直接信任前端传入的字符串，
    一个形如 ``1.1.1.1\\nquit\\nsave`` 的值就能在设备上执行任意命令 —— 这是真实的
    命令注入面，必须校验。

    允许 IP 与主机名：部分客户环境用内网 DNS 名做时间源。
    """
    addr = (address or "").strip()
    if not addr:
        raise ValueError("NTP 服务器地址不能为空")
    if len(addr) > 64:
        raise ValueError("NTP 服务器地址过长（上限 64 字符）")
    # 拒绝任何换行/回车/分号/空格等可用于拆命令的字符
    if any(c in addr for c in "\r\n\t ;|&$`\"'<>\0"):
        raise ValueError(f"NTP 服务器地址含非法字符：{addr!r}")

    try:
        ipaddress.ip_address(addr)
        return addr
    except ValueError:
        pass

    # 主机名（RFC 1123）：字母数字与连字符，标签间用点分隔
    import re
    if not re.fullmatch(r"(?i)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
                        r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*\.?", addr):
        raise ValueError(f"NTP 服务器地址既不是合法 IP 也不是合法主机名：{addr!r}")
    return addr


def validate_clock_mode(mode: str, huawei: bool = False) -> str:
    """校验时钟协议形态：``system``（全局）/ ``mdc``（多 MDC）。"""
    m = (mode or "system").strip().lower()
    if m not in ("system", "mdc"):
        raise ValueError(f"不支持的时钟协议形态：{mode!r}（仅 system / mdc）")
    return m


def is_mdc_device(device) -> bool:
    """是否多 MDC 设备（需要 `clock protocol ntp mdc N` 形态）。

    判据：型号里带 `MDC` 或设备名带 `mdc`。这只是启发式 —— 真机上更可靠的做法是
    让操作人在下发前手动选择形态，所以页面上保留了手动切换入口。
    """
    text = f"{getattr(device, 'model', '') or ''} {getattr(device, 'name', '') or ''}".upper()
    return "MDC" in text


# ---------------------------------------------------------------------------
# 命令模板（默认值 = 现场定稿；页面可编辑覆盖）
# ---------------------------------------------------------------------------
#
# 为什么要有这一层：命令集随型号/版本漂移（H3C MDC 要带 mdc N、华为没有
# clock protocol、现场可能要加 source/priority），写死在代码里等于"每处差异都要发包"。
# 模板化之后：默认值仍是代码常量（部署即正确），现场差异由页面改、留痕、可恢复默认。
#
# 默认值即 2026-09-23 华哥给定的现场定稿，三点刻意的处理保留在这里：
#
#   1. **不带 `ntp-service update-calendar enable`** —— 华哥明确要求去掉。
#      该命令让 NTP 同时校准设备硬件日历，在部分型号上会引入额外写操作；
#      授时的目标是"设备显示时间准"，系统时钟同步即可。
#
#   2. **H3C 下发时先置时区 `clock timezone Beijing add 08:00:00`**（2026-10-08 华哥加）：
#      - H3C 出厂默认时区是 **UTC**，而平台按北京时间展示 —— 不置时区的话，
#        NTP 把系统时钟校准了，设备 `display clock` 显示的仍是 UTC，**差 8 小时**，
#        看上去像"授时没生效"（这正是现场反馈过的那类问题）；
#      - 用**固定北京时间**而不是占位符：模板是"所见即所发"，页面上要能原样看到
#        这条命令；且 NTP 只同步 UTC 时刻、不带时区信息，跨时区客户应按需在页面改；
#      - 放在 `clock protocol ntp` **之前**（先定时区再启用 NTP 客户端），
#        回滚**不动**它（不是本次下发的副作用，撤销会改变设备原有显示口径）。
#
#   3. **华为命令集与 H3C 完全不同**：
#      - `clock protocol ntp`：VRP 无此命令，发过去回 "% Unrecognized command"，
#        回验会把华为设备全判成 failed（AC 那台 MDC 机实测见过该报错）；
#      - 华为真正的「前提开关」是 `undo ntp-service disable`（VRP 的 ntp-service
#        带 disable 状态位，不先 undo 掉，unicast-server 配了也不生效）；
#      - 华为没有 `save force`（force 会被当成文件名报错），`save` 弹出的
#        "Are you sure to continue?[Y/N]" 由 DeviceSession 自动应答 Y，
#        所以**只发 `save`，不要写成两条命令发 `Y`**。
#      - ⚠️ **华为时区命令是 `clock timezone BJ add 08:00:00`**（与 H3C 同名但时区名
#        不能带空格），且 VRP 的时钟形态由 `clock protocol`（无参数 = 手动）
#        与 `ntp-service` 决定 —— 华为是否需要配套时区命令**尚未真机验证**，
#        故本次**不动华为模板**，等华哥现场确认后再加。

VENDOR_H3C = "h3c"
VENDOR_HUAWEI = "huawei"
ACTION_APPLY = "apply"
ACTION_ROLLBACK = "rollback"

# 模板的笛卡尔点，顺序即页面展示顺序
TEMPLATE_KINDS: tuple[tuple[str, str], ...] = (
    (VENDOR_H3C, ACTION_APPLY),
    (VENDOR_H3C, ACTION_ROLLBACK),
    (VENDOR_HUAWEI, ACTION_APPLY),
    (VENDOR_HUAWEI, ACTION_ROLLBACK),
)

# 模板正文里支持的占位符
PLACEHOLDER_ADDRESS = "{address}"
PLACEHOLDER_CLOCK_PROTOCOL = "{clock_protocol}"
PLACEHOLDER_MDC_ID = "{mdc_id}"

MAX_TEMPLATE_LINES = 40
MAX_TEMPLATE_LINE_LEN = 200
MAX_TEMPLATE_BODY_LEN = 8000

DEFAULT_TEMPLATES: dict[tuple[str, str], list[str]] = {
    (VENDOR_H3C, ACTION_APPLY): [
        "system-view",
        "clock timezone Beijing add 08:00:00",
        PLACEHOLDER_CLOCK_PROTOCOL,
        "ntp-service enable",
        f"ntp-service unicast-server {PLACEHOLDER_ADDRESS}",
        "quit",
        "save force",
    ],
    (VENDOR_H3C, ACTION_ROLLBACK): [
        "system-view",
        f"undo ntp-service unicast-server {PLACEHOLDER_ADDRESS}",
        "quit",
        "save force",
    ],
    (VENDOR_HUAWEI, ACTION_APPLY): [
        "system-view",
        "undo ntp-service disable",
        f"ntp-service unicast-server {PLACEHOLDER_ADDRESS}",
        "quit",
        "save",
    ],
    (VENDOR_HUAWEI, ACTION_ROLLBACK): [
        "system-view",
        f"undo ntp-service unicast-server {PLACEHOLDER_ADDRESS}",
        "quit",
        "save",
    ],
}


def vendor_of(device) -> str:
    """设备所属厂商（决定用哪套模板）。"""
    return VENDOR_HUAWEI if is_huawei(device) else VENDOR_H3C


def default_template_body(vendor: str, action: str) -> str:
    """取内置默认模板正文（供页面展示与「恢复默认」）。"""
    return "\n".join(DEFAULT_TEMPLATES[(vendor, action)])


def validate_template_body(body: str) -> list[str]:
    """把模板正文规范成命令行列表（含安全校验），非法模板抛 ``ValueError``。

    这层校验是**安全边界**而不是格式洁癖：模板正文最终会原样下发到设备命令行，
    等价于「管理员可向设备投递任意 CLI」。因此：
      - 逐行去空白、丢空行（设备 CLI 不需要空行，空行还可能被当成回车确认）；
      - 拒绝不可见控制字符（避免夹带回车/退格造成"看到的与执行的不一致"）；
      - 限行数与行长（防误粘贴整段配置把设备刷爆）；
      - **强制包含 ``{address}``** —— 本功能语义就是"把本平台写成设备的 NTP 服务器"，
        模板里没有它，下发后回验（靠回读里找该地址）永远判失败或不可判定，
        与其让人对着一堆"待确认"排查，不如在保存时就拒绝。
    """
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    lines: list[str] = []
    for raw in text.split("\n"):
        s = raw.strip()
        if not s:
            continue
        if len(s) > MAX_TEMPLATE_LINE_LEN:
            raise ValueError(
                f"模板单行过长（上限 {MAX_TEMPLATE_LINE_LEN} 字符）：{s[:40]}…"
            )
        if any(ord(c) < 32 or ord(c) == 127 for c in s):
            raise ValueError("模板含不可见控制字符，请清理后重试")
        lines.append(s)
    if not lines:
        raise ValueError("模板不能为空")
    if len(lines) > MAX_TEMPLATE_LINES:
        raise ValueError(f"模板行数过多（上限 {MAX_TEMPLATE_LINES} 行）")
    if not any(PLACEHOLDER_ADDRESS in ln for ln in lines):
        raise ValueError(
            f"模板必须包含 {PLACEHOLDER_ADDRESS} 占位符"
            "（用于填入本次下发的 NTP 服务器地址，回验也依赖它）"
        )
    return lines


def render_template(lines: list[str], address: str, clock_mode: str = "system",
                    save: bool = True, mdc_id: int = 1,
                    huawei: bool = False) -> list[str]:
    """把模板渲染成最终命令序列。

    - ``{address}`` → 本次下发地址；
    - ``{clock_protocol}`` → ``clock protocol ntp`` / ``clock protocol ntp mdc N``
      （**华为恒为空**：VRP 无此命令，模板里若有该占位符则整行消失）；
    - ``{mdc_id}`` → MDC 编号；
    - 取消「保存配置」时，模板里的 ``save*`` 行自动剔除 —— 这样勾选框对
      自定义模板同样有效，不需要模板作者自己删行、也不会出现"勾了不保存却仍然保存"。
    """
    mode = validate_clock_mode(clock_mode, huawei)
    if huawei:
        clock_line = ""
    elif mode == "mdc":
        clock_line = f"clock protocol ntp mdc {mdc_id}"
    else:
        clock_line = "clock protocol ntp"

    out: list[str] = []
    for raw in lines:
        s = (str(raw)
             .replace(PLACEHOLDER_CLOCK_PROTOCOL, clock_line)
             .replace(PLACEHOLDER_MDC_ID, str(mdc_id))
             .replace(PLACEHOLDER_ADDRESS, address)
             .strip())
        if not s:
            continue
        if not save and s.startswith("save"):
            continue
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# 命令构建（默认模板的便捷入口；自定义模板走 render_template）
# ---------------------------------------------------------------------------

def build_apply_commands(address: str, clock_mode: str = "system",
                         save: bool = True, huawei: bool = False,
                         mdc_id: int = 1) -> list[str]:
    """构建下发命令序列（= 内置默认模板的渲染结果）。

    页面保存过自定义模板时，实际下发走 :func:`resolve_template_lines` + 本函数同款的
    :func:`render_template`；这里只负责"没有自定义"这条默认路径。

    Args:
        address: NTP 服务器地址（平台本机地址）。
        clock_mode: ``system`` = 全局时钟协议；``mdc`` = 指定 MDC。
        save: 是否保存到启动配置（save force / save）。
        huawei: 华为设备（命令集不同）。
        mdc_id: MDC 编号（仅 clock_mode=mdc 时使用）。
    """
    vendor = VENDOR_HUAWEI if huawei else VENDOR_H3C
    return render_template(
        DEFAULT_TEMPLATES[(vendor, ACTION_APPLY)], address, clock_mode, save,
        mdc_id, huawei,
    )


def build_rollback_commands(address: str, clock_mode: str = "system",
                            save: bool = True, huawei: bool = False,
                            restore_clock_protocol: bool = False) -> list[str]:
    """构建回滚命令序列：撤掉目标 NTP 服务器。

    ``restore_clock_protocol``：下发前设备**没有** `clock protocol ntp` 时，
    回滚要不要把时钟协议也改回去？默认**不改** —— 时钟协议是全局设置，
    改回去可能影响设备其它时间相关行为（日志时间戳、证书校验），
    默认保守，只撤 NTP 服务器本身。

    ⚠️ 华为**不恢复** `ntp-service disable`（华哥 2026-09-23 定）：
    `disable` 是很粗的全局关断，可能连带影响设备其它依赖 NTP 的功能。
    与 H3C 的保守策略一致 —— 回滚只撤掉本平台这一条服务器配置。
    """
    vendor = VENDOR_HUAWEI if huawei else VENDOR_H3C
    lines = list(DEFAULT_TEMPLATES[(vendor, ACTION_ROLLBACK)])
    # H3C 才有 `clock protocol`；华为 VRP 无此命令，回滚也不能发 `undo clock protocol`。
    # 插在 system-view 之后、undo unicast-server 之前，与旧实现的顺序逐字一致。
    if restore_clock_protocol and not huawei:
        lines.insert(1, "undo clock protocol")
    return render_template(lines, address, clock_mode, save, 1, huawei)


# ---------------------------------------------------------------------------
# 模板持久化读写（默认模板在代码里，库里只存"被改过的"）
# ---------------------------------------------------------------------------

async def load_command_templates(db) -> dict[tuple[str, str], str]:
    """读取已自定义的模板：``{(vendor, action): body}``；未自定义的键不出现。"""
    rows = (await db.execute(select(NtpCommandTemplate))).scalars().all()
    return {(r.vendor, r.action): r.body for r in rows}


def resolve_template_lines(custom: dict | None, vendor: str, action: str,
                           override_body: str | None = None) -> list[str]:
    """模板优先级：**本次下发的显式覆盖 > 库中已保存 > 内置默认**。

    显式覆盖是给「所见即所发」用的：页面弹窗里改完直接下发，不必先存成默认模板。
    非法模板抛 ``ValueError``（调用方应在连设备**之前**校验，避免整批跑完才报错）。
    """
    if override_body is not None and override_body.strip():
        return validate_template_body(override_body)
    body = (custom or {}).get((vendor, action))
    if body:
        return validate_template_body(body)
    return list(DEFAULT_TEMPLATES[(vendor, action)])


# ---------------------------------------------------------------------------
# 回读解析
# ---------------------------------------------------------------------------

def _extract_ntp_servers(text: str) -> list[str]:
    """从回读文本里提取已配置的 NTP 服务器地址。

    兼容两种承载形态：
      - 配置形态：``ntp-service unicast-server 192.168.1.100``
      - 状态形态：``Reference clock ID  : 192.168.1.100`` 之类
    只取第一段里的合法 IP/主机名 token。
    """
    import re

    found: list[str] = []
    for line in (text or "").splitlines():
        low = line.lower()
        if "unicast-server" in low:
            # ntp-service unicast-server 1.2.3.4 [priority]
            m = re.search(r"unicast-server\s+(\S+)", line, re.I)
        elif "ntp server" in low or "reference clock" in low or "server" in low:
            m = re.search(r"\bserver\s*:?\s*(\S+)", line, re.I)
        else:
            m = None
        if not m:
            continue
        candidate = m.group(1).strip().strip(",;")
        if candidate and candidate not in found:
            found.append(candidate)
    return found


def _configured_servers(snapshot: "NtpSnapshot | str") -> set[str]:
    """回读结果里已配置的 NTP 服务器地址集合（小写）。"""
    if isinstance(snapshot, NtpSnapshot):
        return {s.lower() for s in snapshot.servers}
    return {s.lower() for s in _extract_ntp_servers(snapshot or "")}


def _has_server(snapshot: "NtpSnapshot | str", address: str) -> bool:
    addr = (address or "").strip().lower()
    if not addr:
        return False
    return addr in _configured_servers(snapshot)


class NtpSnapshot:
    """一次 NTP 配置回读的结果 + 「是否可信」的判定依据。

    ``trusted`` 是回验语义的基石：**配置形态的空回读一律不可信** ——
    无法区分「设备上真没配」与「该型号不支持该过滤语法」，当成「没配」会把
    已生效的下发误判成失败，进而诱导操作人重复下发。
    状态形态（``display ntp-service status``）例外：运行时真值，空了也可信。
    """

    __slots__ = ("text", "command", "trusted", "notes", "servers", "kind")

    def __init__(self, text: str, command: str, trusted: bool,
                 notes: list[str] | None = None, servers: list[str] | None = None,
                 kind: str = "config"):
        self.text = text
        self.command = command
        self.trusted = trusted
        self.notes = list(notes or [])
        self.servers = (_extract_ntp_servers(text) if servers is None else list(servers))
        self.kind = kind

    def __repr__(self) -> str:  # pragma: no cover
        return (f"NtpSnapshot(command={self.command!r}, trusted={self.trusted}, "
                f"kind={self.kind!r}, lines={len(self.text.splitlines())}, "
                f"servers={self.servers!r})")


def _is_state_cmd(cmd: str) -> bool:
    return any(s in cmd for s in _STATE_CMDS)


def _snapshot_lines(raw) -> str:
    """把回读输出规范成文本（去 ANSI、去提示符回显、去分页噪音）。"""
    from app.services.device_loghost_service import _normalize_lines

    return _normalize_lines(raw)


async def fetch_ntp_snapshot(session: DeviceSession) -> NtpSnapshot:
    """回读设备的 NTP 配置，按 :data:`SNAPSHOT_CMDS` 由近到远尝试。

    判定规则与 loghost 的快照一致：**可信 = 命令没报错且确实读到了内容**；
    状态类命令额外放宽（空输出也可信）。
    """
    notes: list[str] = []
    text = ""
    servers: list[str] = []
    for cmd in SNAPSHOT_CMDS:
        state_cmd = _is_state_cmd(cmd)
        try:
            out = await session.run(cmd, timeout=SNAPSHOT_TIMEOUT)
        except Exception as e:
            notes.append(f"`{cmd}` 读取失败：{type(e).__name__}: {e}")
            continue
        errs = session.errors_in(out)
        got = _snapshot_lines(out)
        got_servers = _extract_ntp_servers(got)
        if errs:
            notes.append(f"`{cmd}` 报错：{'; '.join(errs)}")
            if not text:
                text, servers = got, got_servers
            continue
        if state_cmd:
            # 状态命令：读到内容即权威（哪怕一个服务器都没有）
            return NtpSnapshot(got, cmd, True, notes, got_servers, "state")
        if got:
            return NtpSnapshot(got, cmd, True, notes, got_servers, "config")
        notes.append(f"`{cmd}` 无输出")
        if not text:
            text, servers = got, got_servers
    last = SNAPSHOT_CMDS[-1]
    return NtpSnapshot(text, last, False, notes, servers,
                       "state" if _is_state_cmd(last) else "config")


async def _run_commands(session: DeviceSession, commands: list[str],
                        timeout: int) -> list[str]:
    """逐条下发并收集错误标记（返回 warnings 文案）。

    与 loghost 的实现一致：保存命令单独放宽空闲阈值并以成功标志收尾
    （设备写盘期间长时间静默，用普通阈值会提前结束读取并留下残余输出）。
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


# ---------------------------------------------------------------------------
# 回验判定（三态）
# ---------------------------------------------------------------------------

def _readback_diag(after: NtpSnapshot) -> str:
    lines = len(after.text.splitlines())
    return (f"（回读：`{after.command}`，{lines} 行；"
            f"看到的 NTP 服务器：{'、'.join(after.servers) if after.servers else '无'}；"
            f"内容：{_summarize(after.text)}）")


def _judge_apply(address: str, after: NtpSnapshot,
                 command_errors: list[str]) -> tuple[str, str | None]:
    """下发后的回验判定 → (state, message)。"""
    if _has_server(after, address):
        return STATE_APPLIED, None
    diag = _readback_diag(after)
    if command_errors:
        return STATE_FAILED, f"下发命令报错，配置很可能未生效：{'; '.join(command_errors)}{diag}"
    if after.trusted:
        seen = "、".join(after.servers) if after.servers else "无"
        where = "运行时状态" if after.kind == "state" else "配置"
        return STATE_FAILED, (
            f"回读 `{after.command}`（{where}）可信，但没有找到目标 NTP 服务器 {address}；"
            f"设备当前生效的 NTP 服务器：{seen}。请登录设备用 `{after.command}` 复核{diag}"
        )
    why = "；".join(after.notes) or "回读不到配置"
    return STATE_UNVERIFIED, (
        f"命令已下发且无报错，但回验不可判定（{why}）{diag}。"
        "该型号可能不支持 `display current-configuration | include ntp` 过滤语法，"
        "或回读方式与设备语法不匹配。请登录设备执行 `display ntp-service status` "
        "人工确认后再决定是否重发"
    )


def _judge_rollback(address: str, after: NtpSnapshot,
                    command_errors: list[str]) -> tuple[str, str | None]:
    """回滚后的回验判定 → (state, message)。"""
    still_there = _has_server(after, address)
    diag = _readback_diag(after)
    if command_errors:
        return STATE_FAILED, f"回滚命令报错：{'; '.join(command_errors)}{diag}"
    if not after.trusted:
        why = "；".join(after.notes) or "回读不到配置"
        return STATE_UNVERIFIED, (
            f"回滚命令已下发且无报错，但回验不可判定（{why}）{diag}。"
            "请登录设备确认目标 NTP 服务器是否已移除"
        )
    if still_there:
        return STATE_FAILED, f"命令已下发但回读仍能查到目标 NTP 服务器 {address}{diag}"
    return STATE_ROLLED_BACK, None


# ---------------------------------------------------------------------------
# 单台下发 / 回滚
# ---------------------------------------------------------------------------

async def apply_to_device(
    device: Device,
    address: str,
    clock_mode: str = "system",
    save: bool = True,
    timeout: int = CONFIG_TIMEOUT,
    templates: dict[tuple[str, str], str] | None = None,
    override_body: str | None = None,
) -> dict:
    """对单台设备下发 NTP 配置。

    返回 ``{state, ok, before, after, commands, warnings, error, clock_mode}``。

    ``templates`` 是库中已保存的模板（``{(vendor, action): body}``）；
    ``override_body`` 是本次下发页面里临时编辑的模板（优先级最高）。
    两者都没有时用内置默认模板 —— 也就是"没动过模板"的机器行为完全不变。
    """
    huawei = is_huawei(device)
    mode = clock_mode if clock_mode != "system" or not is_mdc_device(device) else "mdc"
    lines = resolve_template_lines(templates, vendor_of(device), ACTION_APPLY,
                                   override_body)
    commands = render_template(lines, address, mode, save, 1, huawei)
    result: dict = {
        "device_id": device.id,
        "device_name": device.name,
        "ip": device.ip,
        "state": STATE_FAILED,
        "ok": False,
        "before": None,
        "after": None,
        "commands": "\n".join(commands),
        "warnings": [],
        "error": None,
        "clock_mode": mode,
    }
    session = None
    try:
        session = await open_device_session(device, timeout)
        result["before"] = (await fetch_ntp_snapshot(session)).text
        result["warnings"] = await _run_commands(session, commands, timeout)
        after = await fetch_ntp_snapshot(session)
        result["after"] = after.text
        result["state"], result["error"] = _judge_apply(address, after, result["warnings"])
        result["ok"] = result["state"] == STATE_APPLIED
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        logger.warning("设备 %s(%s) 下发 NTP 配置失败：%s", device.name, device.ip, e)
    finally:
        if session is not None:
            await session.close()
    return result


async def rollback_device(
    device: Device,
    address: str,
    clock_mode: str = "system",
    save: bool = True,
    restore_clock_protocol: bool = False,
    timeout: int = CONFIG_TIMEOUT,
    templates: dict[tuple[str, str], str] | None = None,
) -> dict:
    """对单台设备回滚 NTP 配置（回验同样是三态）。"""
    huawei = is_huawei(device)
    lines = list(resolve_template_lines(templates, vendor_of(device),
                                        ACTION_ROLLBACK))
    if restore_clock_protocol and not huawei:
        # 与 build_rollback_commands 保持同一位置（system-view 之后）
        lines.insert(1, "undo clock protocol")
    commands = render_template(lines, address, clock_mode, save, 1, huawei)
    result: dict = {
        "device_id": device.id,
        "device_name": device.name,
        "ip": device.ip,
        "state": STATE_FAILED,
        "ok": False,
        "before": None,
        "after": None,
        "commands": "\n".join(commands),
        "warnings": [],
        "error": None,
        "clock_mode": clock_mode,
    }
    session = None
    try:
        session = await open_device_session(device, timeout)
        result["before"] = (await fetch_ntp_snapshot(session)).text
        result["warnings"] = await _run_commands(session, commands, timeout)
        after = await fetch_ntp_snapshot(session)
        result["after"] = after.text
        result["state"], result["error"] = _judge_rollback(address, after, result["warnings"])
        result["ok"] = result["state"] == STATE_ROLLED_BACK
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        logger.warning("设备 %s(%s) 回滚 NTP 配置失败：%s", device.name, device.ip, e)
    finally:
        if session is not None:
            await session.close()
    return result


# ---------------------------------------------------------------------------
# 批量编排 + 落库
# ---------------------------------------------------------------------------

def _device_failure(device: Device, message: str) -> dict:
    """构造一条「本台未完成」的结果（字段须与 apply_to_device 完全一致）。"""
    return {
        "device_id": getattr(device, "id", None),
        "device_name": getattr(device, "name", None),
        "ip": getattr(device, "ip", None),
        "state": STATE_FAILED,
        "ok": False, "before": None, "after": None, "commands": None,
        "warnings": [], "error": message, "clock_mode": "system",
    }


def _mark_persist_failure(results: list[dict], exc: Exception, action: str) -> None:
    """留痕落库失败时，把真相写到每台设备结果里而不是让请求 500。

    设备配置**已经改过了**，回 500 只会让操作人以为没生效而重复下发。
    """
    note = (f"{action}记录落库失败（{type(exc).__name__}: {exc}）；"
            "设备配置已变更，请勿重复操作，并联系维护人员核查数据库")
    for res in results:
        res.setdefault("warnings", []).append(note)


async def apply_ntp_to_devices(
    db, devices: list[Device], address: str, clock_mode: str = "system",
    save: bool = True, operator: str | None = None, timeout: int = CONFIG_TIMEOUT,
    overrides: dict[str, str] | None = None,
) -> list[dict]:
    """批量下发并逐台落库记录（成功与失败都记，便于审计与重试）。

    ``overrides``：本次下发的临时模板 ``{"h3c": body, "huawei": body}``，
    覆盖库中已保存的模板（「所见即所发」）。**调用方须先校验**这些模板 ——
    否则非法模板会在逐台连设备时才暴露，整批结果里混着同一句错误、又慢又难读。
    """
    addr = validate_address(address)
    mode = validate_clock_mode(clock_mode)
    if len(devices) > MAX_DEVICES_PER_REQUEST:
        raise ValueError(f"单次最多下发 {MAX_DEVICES_PER_REQUEST} 台设备")
    custom = await load_command_templates(db)
    overrides = overrides or {}

    results = await _run_bounded(
        devices,
        lambda d: apply_to_device(d, addr, mode, save, timeout, custom,
                                  overrides.get(vendor_of(d))),
    )

    now = datetime.now(timezone.utc)
    try:
        for res in results:
            state = res.get("state") or (STATE_APPLIED if res["ok"] else STATE_FAILED)
            db.add(NtpDeviceConfig(
                device_id=res["device_id"],
                device_name=res.get("device_name"),
                device_ip=res.get("ip"),
                server_address=addr,
                clock_mode=res.get("clock_mode") or mode,
                status=state,
                before_config=res.get("before"),
                after_config=res.get("after"),
                commands=res.get("commands"),
                message=res.get("error") or ("; ".join(res.get("warnings") or []) or None),
                # 只有「已确认生效」才记 applied_at；待确认（unverified）不记，
                # 否则状态表里会出现「有时间戳却待确认」的自相矛盾
                applied_at=now if state == STATE_APPLIED else None,
                operator=operator,
            ))
        await db.commit()
    except Exception as e:  # noqa: BLE001
        logger.exception("NTP 下发记录落库失败（设备配置已变更）")
        await _safe_rollback(db)
        _mark_persist_failure(results, e, "下发")
    return results


async def rollback_ntp_on_devices(
    db, devices: list[Device], address: str | None = None, save: bool = True,
    operator: str | None = None, timeout: int = CONFIG_TIMEOUT,
    records: dict[int, NtpDeviceConfig] | None = None,
) -> list[dict]:
    """批量回滚。``records`` 提供各设备的下发记录（用于取地址与形态）。"""
    if len(devices) > MAX_DEVICES_PER_REQUEST:
        raise ValueError(f"单次最多回滚 {MAX_DEVICES_PER_REQUEST} 台设备")
    records = records or {}
    custom = await load_command_templates(db)

    async def _worker(dev: Device) -> dict:
        rec = records.get(dev.id)
        addr = (rec.server_address if rec else None) or address
        if not addr:
            return _device_failure(dev, "未提供 NTP 服务器地址，且该设备没有下发记录，无法回滚")
        return await rollback_device(
            dev, addr,
            clock_mode=(rec.clock_mode if rec else None) or "system",
            save=save, timeout=timeout, templates=custom,
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
                addr = (rec.server_address if rec else None) or address
                db.add(NtpDeviceConfig(
                    device_id=res["device_id"],
                    device_name=res.get("device_name"),
                    device_ip=res.get("ip"),
                    server_address=addr,
                    clock_mode=res.get("clock_mode") or "system",
                    status=state,
                    before_config=res.get("before"),
                    after_config=res.get("after"),
                    commands=res.get("commands"),
                    message=res.get("error") or "; ".join(res.get("warnings") or []) or None,
                    rolled_back_at=now if state == STATE_ROLLED_BACK else None,
                    operator=operator,
                ))
        await db.commit()
    except Exception as e:  # noqa: BLE001
        logger.exception("NTP 回滚记录落库失败（设备配置已变更）")
        await _safe_rollback(db)
        _mark_persist_failure(results, e, "回滚")
    return results


async def latest_records(db, device_ids: list[int]) -> dict[int, NtpDeviceConfig]:
    """取各设备最新一条下发记录（不过滤状态：unverified 也可能已配到设备上）。"""
    if not device_ids:
        return {}
    rows = (await db.execute(
        select(NtpDeviceConfig)
        .where(NtpDeviceConfig.device_id.in_(device_ids))
        .order_by(NtpDeviceConfig.id.desc())
    )).scalars().all()
    out: dict[int, NtpDeviceConfig] = {}
    for r in rows:
        if r.device_id not in out:
            out[r.device_id] = r
    return out


def latest_failure_by_device(rows) -> dict[int | None, NtpDeviceConfig]:
    """从「按 id 倒序的记录行」里取每台设备**最近一条失败**记录。

    为什么失败要单独挑出来，而不是直接看最新一条：失败 → 重试 → 成功是最常见的路径，
    成功记录一落库就把失败原因覆盖掉了。而"这台上次为什么失败"恰恰是决定要不要重试、
    按什么改模板之前最该看的东西 —— 页面上的「查看失败原因」就靠它。

    前提：``rows`` 必须按 id 倒序（越新越靠前），这样"第一条命中的 failed"才是最近的失败。
    回滚失败（rolled_back 之外的 failed 状态）同样计入：那也是真实的下发/回滚失败。
    """
    out: dict[int | None, NtpDeviceConfig] = {}
    for r in rows:
        if r.status == STATE_FAILED and r.device_id not in out:
            out[r.device_id] = r
    return out


# ---------------------------------------------------------------------------
# 平台侧地址候选（复用 loghost 的实现，但优先用 NTP 自己的配置项）
# ---------------------------------------------------------------------------

def local_address_candidates() -> list[str]:
    """本机可被设备访问的候选地址。多网卡时不自动决定，交使用者确认。"""
    from app.services.device_loghost_service import local_address_candidates as _cands

    cands = _cands()
    advertise = (settings.NTP_ADVERTISE_ADDRESS or "").strip()
    if advertise and advertise not in cands:
        cands.insert(0, advertise)
    return cands


def resolve_address(address: str | None) -> str:
    """解析下发给设备的 NTP 服务器地址：入参 → .env 的 NTP_ADVERTISE_ADDRESS。

    留空时抛 ValueError（由路由层转 422），而不是猜一个地址 ——
    多网卡主机猜错网卡，设备会一直同步不上，且从平台侧完全看不出原因。
    """
    addr = (address or settings.NTP_ADVERTISE_ADDRESS or "").strip()
    if not addr:
        raise ValueError(
            "未提供 NTP 服务器地址。请显式指定（设备需能访问该地址），"
            "或在 .env 配置 NTP_ADVERTISE_ADDRESS"
        )
    return validate_address(addr)
