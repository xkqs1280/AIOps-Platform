# -*- coding: utf-8 -*-
"""remote_agent_session.py —— 远程运维会话核心引擎（Linux / Windows 通用）

把「一条 SSH 连接 = 一个带 cwd 记忆的远端 shell 上下文」抽象成线程安全的
会话对象，并提供：命令执行 / sudo+pty / SFTP 读写改 / 目录树增量同步。

本文件是纯引擎（可被 import），也可直接运行 —— 会转交给同目录的分发入口
`remote_agent_mcp.py`（MCP 服务端 + 交互式 CLI）。

关键实现要点（都是踩过的坑）：
  * 哨兵回传 cwd：每条命令尾部注入唯一标记 + pwd，据此实现 cwd 记忆。
    标记**每次调用重新生成**，避免命令自身输出里出现同名文本时解析错位。
  * 整体缓冲一次性解码（utf-8 → gbk → latin-1 探测）：64KB 分片会把多字节
    字符切成两半，逐片 decode 必出乱码；流式回调另走增量解码器。
  * 超时 = 关闭 channel 强杀远端进程（SIGHUP），并置 timed_out 标记。
  * 目录树比对带 LF 归一化容差：远端 CRLF/LF 混用时不会把「仅行尾不同」
    误报成内容差异（.108 上曾因此误报 21 个文件）。
  * 写文本文件支持 newline 归一化（lf/crlf/raw）—— 在 Windows 主机上写
    Linux 脚本必须显式 lf，否则 `$'\\r': command not found`。
"""
from __future__ import annotations

import codecs
import hashlib
import os
import posixpath
import re
import secrets
import stat as _stat
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Iterable, Optional

try:
    import paramiko
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "缺少依赖 paramiko，请先安装：\n"
        "  pip install paramiko\n"
        f"原始错误: {exc}"
    )

__all__ = [
    "RemoteError", "RemoteTimeout", "RemoteAuthError", "RemoteConfigError",
    "ExecResult", "SSHSession", "SessionManager", "HostRegistry",
    "default_config_path", "decode_bytes",
]

MAX_CAPTURE = 8 * 1024 * 1024      # 单流最多缓存 8MB
STREAM_CHUNK = 65536
DEFAULT_TIMEOUT = 60.0
POSIX_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
WINDOWS_SHELLS = {"powershell", "pwsh", "cmd"}

# 目录树同步时默认忽略的噪声（与 Python 侧 _collect_local 的剪枝保持一致，
# 否则远端 __pycache__ 会把「远端独有文件」列表刷成几百条无用信息）
NOISE_DIR_PARTS = {"__pycache__", ".git", ".svn", ".hg", "node_modules", ".venv",
                   "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea",
                   ".vscode", ".tox", ".eggs"}
NOISE_SUFFIXES = (".pyc", ".pyo", ".pyd", ".class", ".swp", ".swx", ".swo", "~")


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class RemoteError(Exception):
    """远程操作失败基类。"""


class RemoteTimeout(RemoteError):
    def __init__(self, message: str, result: "ExecResult | None" = None):
        super().__init__(message)
        self.result = result


class RemoteAuthError(RemoteError):
    """认证 / 连接失败。"""


class RemoteConfigError(RemoteError):
    """主机配置缺失或非法。"""


# --------------------------------------------------------------------------- #
# 编解码
# --------------------------------------------------------------------------- #
def _sniff_encoding(raw: bytes) -> str:
    """按 utf-8 → gbk → latin-1 顺序探测编码（整段探测，不逐片）。"""
    if not raw:
        return "utf-8"
    for enc in ("utf-8", "gbk"):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def decode_bytes(raw: bytes) -> str:
    """整段字节 → 文本（跨分片安全）。"""
    return raw.decode(_sniff_encoding(raw), errors="replace")


class _StreamDecoder:
    """流式增量解码器：首片探测编码后持续喂入，不切断多字节字符。"""

    def __init__(self) -> None:
        self._dec = None

    def feed(self, chunk: bytes) -> str:
        if self._dec is None:
            self._dec = codecs.getincrementaldecoder(_sniff_encoding(chunk))("replace")
        return self._dec.decode(chunk)

    def finish(self) -> str:
        return self._dec.decode(b"", True) if self._dec else ""


# --------------------------------------------------------------------------- #
# 执行结果
# --------------------------------------------------------------------------- #
@dataclass
class ExecResult:
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    cwd: str = ""
    duration: float = 0.0
    timed_out: bool = False
    truncated: bool = False
    command: str = ""
    host: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def output(self) -> str:
        """stdout + stderr 合并（运维排查常用）。"""
        parts = [p for p in (self.stdout, self.stderr) if p]
        return "\n".join(parts)

    def as_dict(self) -> dict:
        return asdict(self)

    def format(self, max_chars: int = 40000) -> str:
        head = (
            f"[exit={self.exit_code}] {self.duration}s"
            f"{' TIMEOUT' if self.timed_out else ''}"
            f"{' TRUNCATED' if self.truncated else ''}"
            f" host={self.host} cwd={self.cwd}"
        )
        body = ""
        if self.stdout:
            body += "\n---- stdout ----\n" + self.stdout
        if self.stderr:
            body += "\n---- stderr ----\n" + self.stderr
        text = head + body
        if len(text) > max_chars:
            keep = max_chars // 2
            text = (
                text[:keep]
                + f"\n... [输出过长，已截断 {len(text) - max_chars} 字符] ...\n"
                + text[-keep:]
            )
        return text


# --------------------------------------------------------------------------- #
# 主机配置
# --------------------------------------------------------------------------- #
CONFIG_ENV = "REMOTE_AGENT_CONFIG"
SUDO_PASS_ENV = "REMOTE_AGENT_SUDO_PASS"


def default_config_path() -> str:
    """默认主机配置路径：优先仓库内 .workbuddy/（已 gitignore），否则用户目录。"""
    env = os.environ.get(CONFIG_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    here = os.path.dirname(os.path.abspath(__file__))
    wb = os.path.join(os.path.dirname(here), ".workbuddy")
    if os.path.isdir(wb):
        return os.path.join(wb, "remote_hosts.json")
    return os.path.join(os.path.expanduser("~"), ".remote_agent", "hosts.json")


_HOST_ALIAS_RE = re.compile(r"^(?P<user>[^@\s]+)@(?P<host>[^:\s]+)(?::(?P<port>\d+))?$")
_HOST_BARE_RE = re.compile(r"^(?P<host>[A-Za-z0-9_.\-]+)(?::(?P<port>\d+))?$")


class HostRegistry:
    """主机登记表：从 JSON 读写，支持 `user@host:port` 形式的临时目标。"""

    def __init__(self, path: Optional[str] = None, autoload: bool = True):
        self.path = os.path.abspath(path or default_config_path())
        self.hosts: dict[str, dict] = {}
        self.default_host: Optional[str] = None
        if autoload:
            self.load()

    # ---------------- 持久化 ----------------
    def load(self) -> None:
        if not os.path.isfile(self.path):
            return
        import json
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            raise RemoteConfigError(f"解析主机配置失败 {self.path}: {exc}") from exc
        raw_hosts = data.get("hosts") or {}
        for name, spec in raw_hosts.items():
            self.hosts[name] = self._normalize(spec)
        self.default_host = data.get("default_host") or self.default_host

    def save(self) -> None:
        import json
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        payload = {"default_host": self.default_host, "hosts": self.hosts}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, self.path)
        try:  # 配置里有口令，权限收紧
            os.chmod(self.path, 0o600)
        except Exception:
            pass

    # ---------------- 基本操作 ----------------
    @staticmethod
    def _normalize(spec) -> dict:
        if isinstance(spec, str):
            return {"host": spec}
        if not isinstance(spec, dict):
            raise RemoteConfigError(f"主机配置项必须是对象或字符串: {spec!r}")
        out = dict(spec)
        out.setdefault("port", 22)
        out["os"] = str(out.get("os", "linux")).lower()
        if "shell" not in out or not out["shell"]:
            out["shell"] = "powershell" if out["os"] == "windows" else "bash"
        out["shell"] = str(out["shell"]).lower()
        return out

    def names(self) -> list[str]:
        return sorted(self.hosts)

    def add(self, name: str, spec) -> dict:
        norm = self._normalize(spec)
        if "host" not in norm:
            raise RemoteConfigError("主机配置必须包含 host 字段")
        self.hosts[name] = norm
        if not self.default_host:
            self.default_host = name
        return norm

    def remove(self, name: str) -> bool:
        existed = self.hosts.pop(name, None) is not None
        if self.default_host == name:
            self.default_host = self.names()[0] if self.hosts else None
        return existed

    # ---------------- 解析 ----------------
    def resolve(self, target: Optional[str] = None) -> tuple[str, dict]:
        """名字 / user@host:port / host → (显示名, 规范化 spec)。"""
        name = target or self.default_host
        if not name:
            raise RemoteConfigError(
                f"未指定主机，且配置里没有 default_host（配置: {self.path}）"
            )
        if name in self.hosts:
            return name, dict(self.hosts[name])

        m = _HOST_ALIAS_RE.match(name)
        if m:
            spec = {"host": m.group("host"), "username": m.group("user")}
            if m.group("port"):
                spec["port"] = int(m.group("port"))
            else:
                spec["port"] = 22
            spec["os"] = "linux"
            spec["shell"] = "bash"
            return name, self._normalize(spec)

        m = _HOST_BARE_RE.match(name)
        if m:
            spec = {"host": m.group("host"), "port": int(m.group("port") or 22),
                    "os": "linux", "shell": "bash"}
            return name, self._normalize(spec)

        raise RemoteConfigError(
            f"未知主机 {name!r}；已配置: {', '.join(self.names()) or '(空)'}"
        )


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
class SSHSession:
    """一条持久 SSH 连接 = 一个远端 shell 上下文（带 cwd 记忆）。"""

    def __init__(self, name: str, spec: dict):
        self.name = name
        self.cfg = dict(spec)
        self.client: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        self._lock = threading.RLock()
        self.cwd: Optional[str] = None
        self.connected_at: float = 0.0
        self.last_used: float = 0.0
        self.call_count = 0

        self.os_type = str(self.cfg.get("os", "linux")).lower()
        self.shell = str(self.cfg.get("shell") or
                         ("powershell" if self.os_type == "windows" else "bash")).lower()
        self.posix = self.shell in POSIX_SHELLS
        if not self.posix and self.shell not in WINDOWS_SHELLS:
            raise RemoteConfigError(f"不支持的 shell: {self.shell}")

    # ------------------------------------------------------------------ #
    # 连接管理
    # ------------------------------------------------------------------ #
    def is_alive(self) -> bool:
        try:
            t = self.client.get_transport() if self.client else None
            return bool(t and t.is_active())
        except Exception:
            return False

    def connect(self, force: bool = False) -> None:
        with self._lock:
            if not force and self.is_alive():
                return
            if self.client is not None:
                self.close()

            spec = self.cfg
            password = _resolve_secret(spec, "password")
            key_file = spec.get("key_file")
            if not password and not key_file:
                raise RemoteConfigError(
                    f"主机 {self.name} 既没有 password 也没有 key_file"
                )

            c = paramiko.SSHClient()
            if spec.get("strict_host_key"):
                c.load_system_host_keys()
                c.set_missing_host_key_policy(paramiko.RejectPolicy())
            else:
                c.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            timeout = float(spec.get("connect_timeout", 15))
            try:
                c.connect(
                    hostname=spec["host"],
                    port=int(spec.get("port", 22)),
                    username=spec.get("username") or os.environ.get("USER") or "root",
                    password=password,
                    key_filename=key_file,
                    passphrase=_resolve_secret(spec, "passphrase"),
                    timeout=timeout,
                    banner_timeout=timeout + 5,
                    auth_timeout=timeout + 5,
                    look_for_keys=bool(key_file),
                    allow_agent=bool(key_file),
                )
            except paramiko.AuthenticationException as exc:
                raise RemoteAuthError(
                    f"{self.name} 认证失败（{spec.get('username')}@{spec['host']}）: {exc}"
                ) from exc
            except Exception as exc:
                raise RemoteAuthError(
                    f"{self.name} 连接失败（{spec['host']}:{spec.get('port', 22)}）: {exc}"
                ) from exc

            c.get_transport().set_keepalive(int(spec.get("keepalive", 30)))
            self.client = c
            self._sftp = None
            self.connected_at = time.time()

            if self.cwd is None:
                self.cwd = spec.get("cwd") or self._probe_cwd()

    def close(self) -> None:
        with self._lock:
            for obj in (self._sftp, self.client):
                try:
                    if obj:
                        obj.close()
                except Exception:
                    pass
            self._sftp = None
            self.client = None

    def _probe_cwd(self) -> str:
        """探测登录后的默认目录。

        必须走**未包装**的裸命令：包装里带 `cd <cwd> || cd /`，此时 self.cwd 还是 None，
        会把探测结果压成 `/`（踩过）。
        """
        try:
            if self.posix:
                cmd = "pwd"
            elif self.shell == "cmd":
                cmd = "echo %CD%"
            else:
                cmd = "(Get-Location).Path"
            r = self._run_raw(cmd, timeout=15)
            for line in reversed(r.stdout.splitlines()):
                if line.strip():
                    return line.strip()
        except Exception:
            pass
        return "/" if self.posix else "C:\\"

    # ------------------------------------------------------------------ #
    # 命令包装
    # ------------------------------------------------------------------ #
    @staticmethod
    def _new_marker() -> str:
        return "__RA_CWD_%s__" % secrets.token_hex(6)

    def _wrap(self, command: str, cwd: str, marker: str, use_pty: bool = False,
              ready_marker: str = "") -> str:
        if self.shell in POSIX_SHELLS:
            q = "'" + (cwd or "/").replace("'", "'\\''") + "'"
            # 分配了 pty 时先关回显：否则行规程会把写到 stdin 的内容（可能是 sudo
            # 口令）原样回显进 stdout。ready_marker 是握手用哨兵 —— 它出现才说明
            # `stty -echo` 已经生效，此时再送 stdin 就不会被回显（消除竞态）。
            pre = ""
            if ready_marker:
                pre = f"stty -echo 2>/dev/null; printf '{ready_marker}\\n'; "
            elif use_pty:
                pre = "stty -echo 2>/dev/null; "
            # 注意：不要用 `{ cmd\n; }` 分组 —— `;` 单独成行是语法错误。
            return (
                f"{pre}cd {q} 2>/dev/null || cd / ;\n"
                f"{command}\n"
                f"__ra_rc=$?\n"
                f"printf '\\n{marker}%s\\n' \"$(pwd)\"\n"
                f"exit $__ra_rc"
            )

        if self.shell in ("powershell", "pwsh"):
            q = (cwd or "C:\\").replace("'", "''")
            return (
                "$ProgressPreference='SilentlyContinue';"
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
                "$OutputEncoding=[System.Text.Encoding]::UTF8;"
                "$global:LASTEXITCODE=$null;"
                f"Set-Location -LiteralPath '{q}' -ErrorAction SilentlyContinue;"
                f"{command};"
                "$__ra_rc = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } "
                "elseif ($?) { 0 } else { 1 };"
                f"Write-Output \"{marker}$((Get-Location).Path)\";"
                "exit $__ra_rc"
            )

        if self.shell == "cmd":
            # 单行链条里 %VAR% 是解析期展开，必须开延迟展开用 !VAR!
            q = (cwd or "C:\\").replace('"', '""')
            return (
                "setlocal EnableDelayedExpansion & chcp 65001>nul & "
                f'cd /d "{q}" & ({command}) & '
                f"set __ra_rc=!errorlevel! & echo {marker}!CD! & exit /b !__ra_rc!"
            )

        raise RemoteConfigError(f"不支持的 shell: {self.shell}")

    # ------------------------------------------------------------------ #
    # 底层执行
    # ------------------------------------------------------------------ #
    def _run_raw(
        self,
        command: str,
        timeout: float = DEFAULT_TIMEOUT,
        on_output: Optional[Callable[[str, str], None]] = None,
        use_pty: bool = False,
        stdin_data: str = "",
        term_size: tuple[int, int] = (200, 50),
        marker: str = "",
        eof: bool = False,
        ready_marker: str = "",
    ) -> ExecResult:
        self.connect()
        t0 = time.time()

        out, err = bytearray(), bytearray()
        dec_out, dec_err = _StreamDecoder(), _StreamDecoder()
        truncated = False
        timed_out = False
        code = -1

        with self._lock:
            try:
                chan = self.client.get_transport().open_session()
            except Exception as exc:
                raise RemoteAuthError(f"{self.name} 打开会话失败: {exc}") from exc

            try:
                chan.settimeout(0.5)
                if use_pty:
                    try:
                        chan.get_pty(term="xterm", width=term_size[0], height=term_size[1])
                    except Exception:
                        pass
                chan.exec_command(command)

                if stdin_data:
                    if ready_marker:
                        # 等 `stty -echo` 真正生效再送 stdin（见 _wrap 注释）；
                        # 等不到也不阻塞，退化成"可能被回显一次"，有遮蔽兜底。
                        self._wait_ready(chan, out, ready_marker,
                                         min(8.0, max(2.0, timeout)))
                    if eof:
                        # pty 下 shutdown_write() 不会把 EOF 交给 tty，得用 Ctrl-D；
                        # 而行规程只在**行缓冲为空**时才把 Ctrl-D 当 EOF，
                        # 所以必须先补一个换行把当前行冲出去，再单独送一个 Ctrl-D。
                        if not stdin_data.endswith("\n"):
                            stdin_data += "\n"
                        stdin_data += "\x04"
                    payload = stdin_data.encode("utf-8")
                    if len(payload) > 4 * 1024 * 1024:
                        raise RemoteError("stdin 数据过大（上限 4MB）")
                    try:
                        chan.sendall(payload)
                    except Exception:
                        pass
                    try:
                        chan.shutdown_write()
                    except Exception:
                        pass

                deadline = time.time() + timeout
                while True:
                    got = False
                    while chan.recv_ready():
                        chunk = chan.recv(STREAM_CHUNK)
                        if not chunk:
                            break
                        got = True
                        if len(out) < MAX_CAPTURE:
                            out += chunk
                            if on_output:
                                on_output("stdout", dec_out.feed(chunk))
                        else:
                            truncated = True

                    while chan.recv_stderr_ready():
                        chunk = chan.recv_stderr(STREAM_CHUNK)
                        if not chunk:
                            break
                        got = True
                        if len(err) < MAX_CAPTURE:
                            err += chunk
                            if on_output:
                                on_output("stderr", dec_err.feed(chunk))
                        else:
                            truncated = True

                    if chan.exit_status_ready() and not got:
                        if not chan.recv_ready() and not chan.recv_stderr_ready():
                            break

                    if time.time() > deadline:
                        timed_out = True
                        break

                    if not got:
                        time.sleep(0.01)

                if on_output:
                    tail = dec_out.finish()
                    if tail:
                        on_output("stdout", tail)
                    tail = dec_err.finish()
                    if tail:
                        on_output("stderr", tail)

                if timed_out:
                    try:
                        chan.close()
                    except Exception:
                        pass
                    code = -1
                else:
                    try:
                        code = chan.recv_exit_status()
                    except Exception:
                        code = -1
            finally:
                if timed_out:
                    try:
                        chan.close()
                    except Exception:
                        pass

        stdout_text = decode_bytes(bytes(out))
        stderr_text = decode_bytes(bytes(err))

        # 带 pty 时回车会被补成 \r\n，且口令可能被回显 → 统一清洗
        if use_pty:
            stdout_text = stdout_text.replace("\r\n", "\n").replace("\r", "")
            stderr_text = stderr_text.replace("\r\n", "\n").replace("\r", "")

        marker = marker or self._new_marker()
        new_cwd = ""
        idx = stdout_text.rfind(marker)
        if idx != -1:
            tail = stdout_text[idx + len(marker):]
            first = tail.splitlines()[0].strip() if tail.splitlines() else ""
            new_cwd = first
            stdout_text = stdout_text[:idx].rstrip("\n")

        if ready_marker:
            stdout_text = stdout_text.replace(ready_marker + "\n", "", 1)
            stdout_text = stdout_text.replace(ready_marker, "")

        for secret_value in self._secrets_to_mask():
            if secret_value:
                stdout_text = stdout_text.replace(secret_value, "******")
                stderr_text = stderr_text.replace(secret_value, "******")

        return ExecResult(
            exit_code=code,
            stdout=stdout_text,
            stderr=stderr_text,
            cwd=new_cwd,
            duration=round(time.time() - t0, 3),
            timed_out=timed_out,
            truncated=truncated,
            command=command,
            host=self.name,
        )

    @staticmethod
    def _wait_ready(chan, sink: bytearray, marker: str, deadline_sec: float) -> bool:
        """等到远端回显 ready 哨兵（说明 `stty -echo` 已生效）再继续。

        握手期间读到的东西全部丢进 sink（不触发回调），哨兵随后统一从输出里剔除。
        """
        needle = marker.encode("utf-8")
        deadline = time.time() + deadline_sec
        while time.time() < deadline:
            while chan.recv_ready():
                chunk = chan.recv(STREAM_CHUNK)
                if not chunk:
                    break
                if len(sink) < MAX_CAPTURE:
                    sink += chunk
            while chan.recv_stderr_ready():
                chan.recv_stderr(STREAM_CHUNK)
            if needle in sink:
                return True
            if chan.exit_status_ready() and not chan.recv_ready():
                return False
            time.sleep(0.01)
        return False

    def _secrets_to_mask(self) -> Iterable[str]:
        """需要从输出里遮蔽的敏感串（口令等）。"""
        for key in ("sudo_password", SUDO_PASS_ENV, "password"):
            v = self.cfg.get(key)
            if isinstance(v, str) and len(v) >= 4:
                yield v

    def _exec_wrapped(self, command: str, timeout: float = DEFAULT_TIMEOUT,
                      on_output=None, use_pty: bool = False,
                      stdin_data: str = "", eof: bool = False) -> ExecResult:
        # 必须先建连接：cwd 的初始化（配置里的 cwd 或登录 home）发生在 connect() 里，
        # 否则首次调用会拿到 None 而被当成 `/`（踩过）。
        self.connect()
        marker = self._new_marker()
        cwd = self.cwd or ("/" if self.posix else "C:\\")
        # pty + stdin 时才做就绪握手（非 pty 没有回显问题，白等会拖慢每条命令）
        ready = self._new_marker() if (use_pty and stdin_data) else ""
        wrapped = self._wrap(command, cwd, marker, use_pty=use_pty, ready_marker=ready)
        return self._run_raw(wrapped, timeout=timeout, on_output=on_output,
                             use_pty=use_pty, stdin_data=stdin_data, marker=marker,
                             eof=eof, ready_marker=ready)

    # ------------------------------------------------------------------ #
    # 对外 exec
    # ------------------------------------------------------------------ #
    def exec(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        sudo: bool = False,
        pty: bool = False,
        stdin: str = "",
        on_output: Optional[Callable[[str, str], None]] = None,
        env: Optional[dict] = None,
    ) -> ExecResult:
        """执行一条命令。

        sudo=True  → 用 `sudo -S -p ''` 并把口令写到 stdin。

            默认**不**分配 pty（实测 .108/.106 都能跑）：pty 会带来三件麻烦事 ——
            行规程会在 `stty -echo` 生效前把明文口令回显进 stdout、sudoers 若开了
            `pwfeedback` 还会把口令打成一行 `*`、并且输出被改成 CRLF。
            只有目标机 sudoers 配了 `Defaults requiretty` 时才需要 pty，
            那种主机单独把配置里的 `sudo_pty` 设成 true 即可。

        pty=True   → 强制分配伪终端（交互式程序、需要 tty 的命令）。
        stdin      → 喂给命令的标准输入（与 sudo 口令拼接，sudo 口令在前）。
        env        → 命令前导环境变量（POSIX 用 `env` 前缀）。
        """
        cmd = command
        stdin_data = stdin or ""
        user_stdin = bool(stdin)

        if env:
            prefix = " ".join(f"{k}={_shell_quote(v)}" for k, v in env.items())
            if self.posix:
                cmd = f"env {prefix} {cmd}"
            else:
                sets = "; ".join(f"$env:{k}='{str(v).replace(chr(39), chr(39) * 2)}'"
                                 for k, v in env.items())
                cmd = f"{sets}; {cmd}"

        if sudo:
            if not self.posix:
                raise RemoteConfigError(f"{self.name} 是 {self.shell}，不支持 sudo")
            pwd = self._sudo_password()
            # 整条命令必须作为**一个** shell 调用交给 sudo：若直接写
            # `sudo -S -H a; b`，分号由外层 shell 解析，只有 a 提权、b 仍是普通用户
            # （踩过：`sudo sed -i ...; systemctl daemon-reload` 里 sed 报权限不足、
            # daemon-reload 报 polkit 需要交互认证，而返回码看着还挺正常）。
            cmd = f"sudo -S -p '' -H sh -c {_shell_quote(cmd)}"
            if pwd:
                stdin_data = pwd + "\n" + stdin_data

        if cwd:
            self.cwd = cwd

        use_pty = bool(pty) or (sudo and bool(self.cfg.get("sudo_pty", False)))
        r = self._exec_wrapped(cmd, timeout=timeout, on_output=on_output,
                               use_pty=use_pty, stdin_data=stdin_data,
                               eof=(use_pty and user_stdin))
        if sudo and r.stderr.startswith("\n"):
            # sudo 读完口令后会补一个换行，纯噪声
            r.stderr = r.stderr.lstrip("\n")
        if r.cwd:
            self.cwd = r.cwd
        self.last_used = time.time()
        self.call_count += 1
        return r

    def _sudo_password(self) -> str:
        for key in ("sudo_password", "password"):
            v = self.cfg.get(key)
            if isinstance(v, str) and v:
                return v
        return os.environ.get(SUDO_PASS_ENV, "")

    def run_ok(self, command: str, **kw) -> str:
        """执行并断言成功；失败直接抛 RemoteError（省去到处判 rc）。"""
        r = self.exec(command, **kw)
        if not r.ok:
            raise RemoteError(f"命令失败 exit={r.exit_code}: {command}\n{r.format()}")
        return r.stdout

    # ------------------------------------------------------------------ #
    # SFTP
    # ------------------------------------------------------------------ #
    def sftp(self) -> paramiko.SFTPClient:
        self.connect()
        with self._lock:
            if self._sftp is None:
                self._sftp = self.client.open_sftp()
            return self._sftp

    # -------- 读 --------
    def read_file(self, path: str, start_line: int = 1, max_lines: int = 500,
                  encoding: Optional[str] = None) -> str:
        """读文本；start_line 支持负数（从尾部数，-200 = 最后 200 行）。

        max_lines<=0 表示不限制行数（整文件读回）。
        """
        s = self.sftp()
        with s.open(path, "rb") as f:
            data = f.read()
        text = data.decode(encoding or _sniff_encoding(data), errors="replace")
        lines = text.splitlines()
        if start_line < 0:
            start_line = max(1, len(lines) + start_line + 1)
        end = len(lines) if max_lines <= 0 else start_line - 1 + max_lines
        return "\n".join(lines[start_line - 1: end])

    def read_text(self, path: str, encoding: Optional[str] = None) -> str:
        return self.read_file(path, start_line=1, max_lines=0, encoding=encoding)

    def read_bytes(self, path: str) -> bytes:
        s = self.sftp()
        with s.open(path, "rb") as f:
            return f.read()

    def tail(self, path: str, lines: int = 200) -> str:
        return self.read_file(path, start_line=-lines, max_lines=lines)

    def list_dir(self, path: str = ".") -> list[dict]:
        s = self.sftp()
        items = []
        for attr in s.listdir_attr(path):
            items.append({
                "name": attr.filename,
                "size": attr.st_size,
                "is_dir": _stat.S_ISDIR(attr.st_mode),
                "is_link": _stat.S_ISLNK(attr.st_mode),
                "mode": oct(attr.st_mode & 0o7777),
                "mtime": attr.st_mtime,
            })
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return items

    def stat_path(self, path: str) -> dict:
        s = self.sftp()
        a = s.stat(path)
        return {
            "path": path,
            "size": a.st_size,
            "is_dir": _stat.S_ISDIR(a.st_mode),
            "mode": oct(a.st_mode & 0o7777),
            "mtime": a.st_mtime,
            "atime": a.st_atime,
        }

    def exists(self, path: str) -> bool:
        try:
            self.sftp().stat(path)
            return True
        except IOError:
            return False

    # -------- 写 --------
    def write_file(self, path: str, content: str, append: bool = False,
                   newline: str = "raw", mode: Optional[str] = None,
                   encoding: str = "utf-8") -> dict:
        """写文本。newline: raw 原样 | lf 强制 \\n | crlf 强制 \\r\\n。

        在 Windows 主机上写 Linux 脚本（*.sh/*.py/*.conf）必须显式 newline="lf"，
        否则远端报 `$'\\r': command not found`。
        """
        if newline == "lf":
            content = content.replace("\r\n", "\n").replace("\r", "\n")
        elif newline == "crlf":
            content = content.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
        elif newline != "raw":
            raise RemoteConfigError(f"newline 只能是 raw/lf/crlf，收到 {newline!r}")

        data = content.encode(encoding)
        self._ensure_remote_dir(path)
        s = self.sftp()
        with s.open(path, "ab" if append else "wb") as f:
            f.write(data)
        if mode:
            try:
                s.chmod(path, int(mode, 8) if isinstance(mode, str) else mode)
            except Exception:
                pass
        return {"path": path, "bytes": len(data), "append": append, "newline": newline}

    def write_bytes(self, path: str, data: bytes) -> dict:
        self._ensure_remote_dir(path)
        s = self.sftp()
        with s.open(path, "wb") as f:
            f.write(data)
        return {"path": path, "bytes": len(data)}

    def replace_in_file(self, path: str, old: str, new: str, count: int = 0,
                        encoding: Optional[str] = None) -> dict:
        """精确串替换（先写临时文件再原子改名）。count=0 表示全替换。"""
        s = self.sftp()
        with s.open(path, "rb") as f:
            data = f.read()
        enc = encoding or _sniff_encoding(data)
        text = data.decode(enc, errors="replace")
        hits = text.count(old)
        if hits == 0:
            return {"path": path, "replaced": 0, "hits": 0}
        new_text = text.replace(old, new) if count <= 0 else text.replace(old, new, count)
        replaced = hits if count <= 0 else min(hits, count)
        tmp = f"{path}.ra_tmp_{secrets.token_hex(4)}"
        with s.open(tmp, "wb") as f:
            f.write(new_text.encode(enc))
        try:
            s.posix_rename(tmp, path)
        except (IOError, AttributeError):
            try:
                s.remove(path)
            except IOError:
                pass
            s.rename(tmp, path)
        return {"path": path, "replaced": replaced, "hits": hits}

    def mkdir_p(self, path: str) -> None:
        self._ensure_remote_dir(posixpath.join(path, "_"))

    def chmod(self, path: str, mode) -> None:
        self.sftp().chmod(path, int(mode, 8) if isinstance(mode, str) else mode)

    def remove(self, path: str, recursive: bool = False) -> None:
        s = self.sftp()
        attr = s.stat(path)
        if _stat.S_ISDIR(attr.st_mode):
            if not recursive:
                raise RemoteError(f"{path} 是目录，需 recursive=True")
            for item, is_dir in self._walk_remote(path):
                if is_dir:
                    s.rmdir(item)
                else:
                    s.remove(item)
            s.rmdir(path)
        else:
            s.remove(path)

    def _walk_remote(self, root: str) -> list[tuple[str, bool]]:
        """后序遍历（先文件后子目录再自身），返回 [(路径, 是否目录)]，便于安全删除。"""
        s = self.sftp()
        entries: list[tuple[str, bool]] = []
        for attr in s.listdir_attr(root):
            full = posixpath.join(root, attr.filename)
            if _stat.S_ISDIR(attr.st_mode):
                entries.extend(self._walk_remote(full))
                entries.append((full, True))
            else:
                entries.append((full, False))
        return entries

    # -------- 目录与传输 --------
    def _ensure_remote_dir(self, remote_path: str) -> None:
        raw = remote_path.replace("\\", "/")
        if self.shell == "cmd":
            raw = raw.replace("/", "\\")
        drive = ""
        m = re.match(r"^([A-Za-z]:)", raw)
        if m:
            drive, raw = m.group(1), raw[2:]
        d = posixpath.dirname(raw)
        if not d:
            return
        s = self.sftp()
        cur = drive
        for part in [p for p in d.split("/") if p]:
            cur = f"{cur}/{part}" if cur else f"/{part}"
            try:
                s.stat(cur)
            except IOError:
                try:
                    s.mkdir(cur)
                except IOError:
                    # 部分 Windows OpenSSH 需走 shell 建目录
                    self._mkdir_fallback(cur)

    def _mkdir_fallback(self, path: str) -> None:
        try:
            if self.posix:
                self._exec_wrapped(f"mkdir -p {_shell_quote(path)}", timeout=20)
            else:
                q = path.replace("'", "''")
                self._exec_wrapped(
                    f"New-Item -ItemType Directory -Force -Path '{q}' | Out-Null",
                    timeout=20,
                )
        except Exception:
            pass

    def upload(self, local: str, remote: str, preserve_mtime: bool = True) -> dict:
        self._ensure_remote_dir(remote)
        s = self.sftp()
        s.put(local, remote, confirm=True)
        try:
            if preserve_mtime:
                st = os.stat(local)
                s.utime(remote, (st.st_atime, st.st_mtime))
        except Exception:
            pass
        return {"local": local, "remote": remote,
                "bytes": os.path.getsize(local)}

    def download(self, remote: str, local: str, preserve_mtime: bool = True) -> dict:
        os.makedirs(os.path.dirname(os.path.abspath(local)) or ".", exist_ok=True)
        s = self.sftp()
        s.get(remote, local)
        if preserve_mtime:
            try:
                a = s.stat(remote)
                os.utime(local, (a.st_atime, a.st_mtime))
            except Exception:
                pass
        return {"remote": remote, "local": local,
                "bytes": os.path.getsize(local)}

    # -------- 目录树比对与同步 --------
    @staticmethod
    def _local_hashes(path: str) -> tuple[str, str]:
        with open(path, "rb") as f:
            raw = f.read()
        return (_md5(raw), _md5(raw.replace(b"\r\n", b"\n")))

    def remote_md5_map(self, remote_dir: str, ignore_noise: bool = True) -> dict[str, str]:
        """{相对路径: md5}，一条命令取回整棵树的哈希。"""
        if self.posix:
            cmd = (
                f"cd {_shell_quote(remote_dir)} && "
                "find . -type f -print0 2>/dev/null | xargs -0 -r md5sum 2>/dev/null"
            )
            r = self.exec(cmd, timeout=180)
            out = {}
            for line in r.stdout.splitlines():
                line = line.strip()
                if not line or "  " not in line:
                    continue
                h, _, p = line.partition("  ")
                p = p.strip()
                if p.startswith("./"):
                    p = p[2:]
                if ignore_noise and _is_noise(p):
                    continue
                out[p] = h.strip()
            return out

        q = remote_dir.replace("'", "''")
        cmd = (
            f"$base='{q}'; "
            f"Get-ChildItem -LiteralPath $base -Recurse -File -ErrorAction SilentlyContinue | "
            "ForEach-Object { $rel=$_.FullName.Substring($base.Length).TrimStart('\\')"
            ".Replace('\\','/'); "
            "\"$((Get-FileHash -LiteralPath $_.FullName -Algorithm MD5).Hash.ToLower())  $rel\" }"
        )
        r = self.exec(cmd, timeout=300)
        out = {}
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line or "  " not in line:
                continue
            h, _, p = line.partition("  ")
            p = p.strip()
            if p.startswith("./"):
                p = p[2:]
            if ignore_noise and _is_noise(p):
                continue
            out[p] = h.strip()
        return out

    def put_tree(
        self,
        local_dir: str,
        remote_dir: str,
        include: Optional[list[str]] = None,
        exclude: Optional[list[str]] = None,
        dry_run: bool = False,
        delete_extra: bool = False,
        lf_tolerant: bool = True,
        ignore_noise: bool = True,
        on_progress: Optional[Callable[[str, str], None]] = None,
    ) -> dict:
        """把本地目录增量同步到远端：只传内容真正有差异的文件。

        lf_tolerant=True 时，「仅行尾不同」视为相同（远端 CRLF/LF 混用场景）。
        delete_extra 默认 False —— 绝不擅自删远端文件。
        ignore_noise=True 时跳过 __pycache__/.git/node_modules 等缓存目录。
        """
        local_map = self._collect_local(local_dir, include, exclude,
                                        ignore_noise=ignore_noise)
        remote_map = self.remote_md5_map(remote_dir, ignore_noise=ignore_noise)

        changed, added, unchanged = [], [], []
        for rel, full in sorted(local_map.items()):
            h_raw, h_lf = self._local_hashes(full)
            if rel not in remote_map:
                added.append(rel)
            elif _hashes_same(remote_map[rel], h_raw, h_lf, lf_tolerant):
                unchanged.append(rel)
            else:
                changed.append(rel)

        extra = [r for r in sorted(remote_map) if r not in local_map]
        result = {
            "direction": "push",
            "local_dir": local_dir, "remote_dir": remote_dir,
            "local_count": len(local_map), "remote_count": len(remote_map),
            "unchanged": unchanged, "changed": changed, "added": added,
            "extra_remote": extra, "uploaded": [], "errors": [],
            "deleted": [], "dry_run": dry_run,
        }

        todo = sorted(changed + added)
        if dry_run:
            return result

        for rel in todo:
            remote_path = posixpath.join(remote_dir, rel)
            try:
                self.upload(local_map[rel], remote_path)
                result["uploaded"].append(rel)
                if on_progress:
                    on_progress(rel, "uploaded")
            except Exception as exc:
                result["errors"].append({"file": rel, "error": str(exc)})
                if on_progress:
                    on_progress(rel, f"error: {exc}")

        if delete_extra:
            for rel in extra:
                try:
                    self.remove(posixpath.join(remote_dir, rel))
                    result["deleted"].append(rel)
                except Exception as exc:
                    result["errors"].append({"file": rel, "error": str(exc)})

        return result

    def get_tree(
        self,
        remote_dir: str,
        local_dir: str,
        include: Optional[list[str]] = None,
        exclude: Optional[list[str]] = None,
        dry_run: bool = False,
        lf_tolerant: bool = True,
        ignore_noise: bool = True,
        on_progress: Optional[Callable[[str, str], None]] = None,
    ) -> dict:
        remote_map = self.remote_md5_map(remote_dir, ignore_noise=ignore_noise)
        rel_filtered = {r: h for r, h in remote_map.items()
                        if self._match_filters(r, include, exclude)}

        changed, added, unchanged = [], [], []
        for rel, h in sorted(rel_filtered.items()):
            local_path = os.path.join(local_dir, *rel.split("/"))
            if not os.path.isfile(local_path):
                added.append(rel)
                continue
            h_raw, h_lf = self._local_hashes(local_path)
            if _hashes_same(h, h_raw, h_lf, lf_tolerant):
                unchanged.append(rel)
            else:
                changed.append(rel)

        result = {"direction": "pull", "remote_dir": remote_dir, "local_dir": local_dir,
                  "remote_count": len(rel_filtered), "local_count": 0,
                  "unchanged": unchanged, "changed": changed, "added": added,
                  "downloaded": [], "errors": [], "dry_run": dry_run}
        try:
            result["local_count"] = len(self._collect_local(local_dir, include, exclude,
                                                            ignore_noise=ignore_noise))
        except OSError:
            pass
        if dry_run:
            return result

        for rel in sorted(changed + added):
            local_path = os.path.join(local_dir, *rel.split("/"))
            try:
                self.download(posixpath.join(remote_dir, rel), local_path)
                result["downloaded"].append(rel)
                if on_progress:
                    on_progress(rel, "downloaded")
            except Exception as exc:
                result["errors"].append({"file": rel, "error": str(exc)})
                if on_progress:
                    on_progress(rel, f"error: {exc}")
        return result

    @staticmethod
    def _match_filters(rel: str, include, exclude) -> bool:
        if include and not any(re.search(p, rel) for p in include):
            return False
        if exclude and any(re.search(p, rel) for p in exclude):
            return False
        return True

    @classmethod
    def _collect_local(cls, root: str, include, exclude,
                       ignore_noise: bool = True) -> dict[str, str]:
        out: dict[str, str] = {}
        root = os.path.abspath(root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", ".git", "node_modules", ".venv")]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root).replace("\\", "/")
                if ignore_noise and _is_noise(rel):
                    continue
                if cls._match_filters(rel, include, exclude):
                    out[rel] = full
        return out

    # ------------------------------------------------------------------ #
    # 探活
    # ------------------------------------------------------------------ #
    def probe(self) -> dict:
        info: dict = {"host": self.name, "target": self.cfg.get("host"),
                      "port": self.cfg.get("port", 22),
                      "username": self.cfg.get("username"),
                      "os": self.os_type, "shell": self.shell}
        t0 = time.time()
        try:
            self.connect()
            info["connected"] = True
            info["connect_ms"] = int((time.time() - t0) * 1000)
        except Exception as exc:
            info["connected"] = False
            info["error"] = str(exc)
            return info

        if self.posix:
            r = self.exec(
                "echo H=$(hostname); echo U=$(uname -sr); "
                "echo V=$( . /etc/os-release 2>/dev/null && echo $PRETTY_NAME ); "
                "echo S=$(uptime -p 2>/dev/null || uptime); "
                "echo D=$(df -h / | awk 'NR==2{print $2\" total / \"$5\" used\"}')",
                timeout=20)
            for line in r.stdout.splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    info[{"H": "hostname", "U": "kernel", "V": "distro",
                          "S": "uptime", "D": "disk"}.get(k.strip(), k.strip())] = v.strip()
            s = self.exec("id -un; id -Gn | head -1; "
                          "(sudo -n true 2>/dev/null && echo SUDO_NOPASS) || echo SUDO_NEED_PASS",
                          timeout=20)
            lines = [x.strip() for x in s.stdout.splitlines() if x.strip()]
            info["login_user"] = lines[0] if lines else ""
            info["sudo"] = "nopasswd" if "SUDO_NOPASS" in s.stdout else "password"
        else:
            r = self.exec(
                "Write-Output \"H=$env:COMPUTERNAME\";"
                "Write-Output \"V=$([Environment]::OSVersion.VersionString)\";"
                "Write-Output \"S=$((Get-CimInstance Win32_OperatingSystem).LastBootUpTime)\";"
                "Write-Output \"P=$([Security.Principal.WindowsIdentity]::GetCurrent().Name)\"",
                timeout=40)
            for line in r.stdout.splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    info[{"H": "hostname", "V": "os_version", "S": "boot_time",
                          "P": "login_user"}.get(k.strip(), k.strip())] = v.strip()
            a = self.exec("[bool]([Security.Principal.WindowsPrincipal]"
                          "[Security.Principal.WindowsIdentity]::GetCurrent())"
                          ".IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)",
                          timeout=30)
            info["admin"] = a.stdout.strip().lower().startswith("true")
        info["cwd"] = self.cwd
        return info

    # ------------------------------------------------------------------ #
    # systemd 快捷操作（Linux）
    # ------------------------------------------------------------------ #
    def systemctl(self, action: str, unit: str, lines: int = 80,
                  sudo: bool = True) -> ExecResult:
        if not self.posix:
            raise RemoteConfigError("systemctl 仅适用于 Linux 主机")
        action = action.lower()
        if action == "status":
            cmd = f"systemctl status {_shell_quote(unit)} --no-pager -l 2>&1 | head -n {int(lines)}"
            return self.exec(cmd, timeout=30)
        if action == "journal":
            cmd = (f"(journalctl -u {_shell_quote(unit)} -n {int(lines)} --no-pager 2>&1 "
                   "|| tail -n %d /var/log/syslog)" % int(lines))
            return self.exec(cmd, timeout=40, sudo=sudo)
        if action == "is-active":
            return self.exec(f"systemctl is-active {_shell_quote(unit)}", timeout=20)
        if action in ("start", "stop", "restart", "reload", "enable", "disable", "daemon-reload"):
            if action == "daemon-reload":
                cmd = "systemctl daemon-reload"
            else:
                cmd = f"systemctl {action} {_shell_quote(unit)} && sleep 1 && " \
                      f"systemctl is-active {_shell_quote(unit)}"
            return self.exec(cmd, timeout=90, sudo=sudo)
        raise RemoteConfigError(
            "action 支持: status/journal/is-active/start/stop/restart/reload/enable/disable/daemon-reload")


# --------------------------------------------------------------------------- #
# 会话管理
# --------------------------------------------------------------------------- #
class SessionManager:
    """按名字复用 SSSession，线程安全。"""

    def __init__(self, registry: Optional[HostRegistry] = None):
        self.registry = registry or HostRegistry()
        self._sessions: dict[str, SSHSession] = {}
        self._lock = threading.RLock()

    def get(self, target: Optional[str] = None) -> SSHSession:
        name, spec = self.registry.resolve(target)
        key = name if name in self.registry.hosts else f"{spec.get('username')}@{spec['host']}"
        with self._lock:
            sess = self._sessions.get(key)
            if sess is None:
                sess = SSHSession(name, spec)
                self._sessions[key] = sess
            return sess

    def close(self, target: Optional[str] = None) -> bool:
        with self._lock:
            if target is None:
                self.close_all()
                return True
            try:
                _, spec = self.registry.resolve(target)
            except RemoteConfigError:
                return False
            key = target if target in self.registry.hosts else \
                f"{spec.get('username')}@{spec['host']}"
            sess = self._sessions.pop(key, None)
            if sess:
                sess.close()
                return True
            return False

    def close_all(self) -> int:
        with self._lock:
            n = len(self._sessions)
            for sess in self._sessions.values():
                sess.close()
            self._sessions.clear()
            return n

    def list(self) -> list[dict]:
        with self._lock:
            out = []
            for key, sess in self._sessions.items():
                out.append({
                    "key": key, "host": sess.name,
                    "target": sess.cfg.get("host"),
                    "username": sess.cfg.get("username"),
                    "os": sess.os_type, "shell": sess.shell,
                    "alive": sess.is_alive(), "cwd": sess.cwd,
                    "calls": sess.call_count,
                    "idle_sec": round(time.time() - sess.last_used, 1) if sess.last_used else None,
                })
            return out


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _shell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def _hashes_same(remote_hash: str, local_raw: str, local_lf: str,
                 lf_tolerant: bool) -> bool:
    """远端哈希与本地（原始 / LF 归一化）任一相等即视为内容相同。

    远端 CRLF/LF 混用时（.108 就是：新脚本写 LF、历史批量部署留 CRLF），
    只比原始字节会把「仅行尾不同」误报成内容差异。
    """
    if remote_hash == local_raw:
        return True
    return lf_tolerant and remote_hash == local_lf


def _is_noise(rel_path: str) -> bool:
    """判断相对路径是否属于同步噪声（缓存、VCS、依赖目录等）。"""
    parts = rel_path.replace("\\", "/").split("/")
    if any(p in NOISE_DIR_PARTS for p in parts[:-1]):
        return True
    name = parts[-1] if parts else ""
    if name in NOISE_DIR_PARTS or name == ".DS_Store":
        return True
    return name.endswith(NOISE_SUFFIXES)


def _resolve_secret(spec: dict, key: str) -> Optional[str]:
    """取值顺序：spec[key] → spec[key + '_env'] 指定的环境变量。"""
    val = spec.get(key)
    if isinstance(val, str) and val:
        return val
    env_name = spec.get(f"{key}_env")
    if env_name:
        return os.environ.get(str(env_name))
    return None


if __name__ == "__main__":
    # 引擎本身不含入口，转交给同目录的分发入口（保持「两个文件都能直接跑」）
    import runpy
    import sys

    entry = os.path.join(os.path.dirname(os.path.abspath(__file__)), "remote_agent_mcp.py")
    if os.path.isfile(entry):
        sys.argv[0] = entry
        runpy.run_path(entry, run_name="__main__")
    else:
        raise SystemExit(
            "remote_agent_session.py 是引擎模块，请运行同目录的 remote_agent_mcp.py "
            "（MCP 服务端 / 交互式 CLI）"
        )
