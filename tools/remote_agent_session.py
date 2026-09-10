# remote_agent/session.py
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import paramiko

CWD_MARK = "__CWD__"
MAX_CAPTURE = 8 * 1024 * 1024  # 单条命令最多缓存 8MB


def _decode(b: bytes) -> str:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


@dataclass
class ExecResult:
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    cwd: str = ""
    duration: float = 0.0
    timed_out: bool = False


class SSHSession:
    """一条持久 SSH 连接 = 一个远端 shell 上下文（带 cwd 记忆）"""

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.cfg = cfg
        self.client: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        self._lock = threading.RLock()
        self.cwd: Optional[str] = None

        self.os_type = cfg.get("os", "linux").lower()
        self.shell = cfg.get("shell") or ("bash" if self.os_type == "linux" else "powershell")

    # ---------------- 连接管理 ----------------
    def connect(self) -> None:
        with self._lock:
            t = self.client.get_transport() if self.client else None
            if t and t.is_active():
                return

            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(
                hostname=self.cfg["host"],
                port=int(self.cfg.get("port", 22)),
                username=self.cfg["username"],
                password=self.cfg.get("password"),
                key_filename=self.cfg.get("key_file"),
                timeout=self.cfg.get("connect_timeout", 15),
                banner_timeout=20,
                auth_timeout=20,
                look_for_keys=False,
                allow_agent=False,
            )
            c.get_transport().set_keepalive(30)  # 防止 NAT 超时断连
            self.client = c
            self._sftp = None

            if self.cwd is None:
                self.cwd = self.cfg.get("cwd") or self._probe_home()

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

    def _probe_home(self) -> str:
        cmd = "pwd" if self.shell == "bash" else "(Get-Location).Path"
        r = self._run_raw(cmd, timeout=10)
        lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        return lines[-1] if lines else ("/" if self.shell == "bash" else "C:\\")

    # ---------------- 命令包装（关键：cwd 记忆 + exit code 保真） ----------------
    def _wrap(self, command: str, cwd: str) -> str:
        if self.shell == "bash":
            q = "'" + cwd.replace("'", "'\\''") + "'"
            return (
                f"cd {q} 2>/dev/null || cd / ;\n"
                f"{command}\n"
                f"__rc=$?\n"
                f"printf '\\n{CWD_MARK}%s\\n' \"$(pwd)\"\n"
                f"exit $__rc"
            )

        if self.shell == "powershell":
            q = cwd.replace("'", "''")
            return (
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
                "$OutputEncoding=[System.Text.Encoding]::UTF8;"
                f"Set-Location -LiteralPath '{q}' -ErrorAction SilentlyContinue;"
                f"{command};"
                "$__rc=$LASTEXITCODE; if ($null -eq $__rc) { $__rc = 0 };"
                f"Write-Output \"{CWD_MARK}$((Get-Location).Path)\";"
                "exit $__rc"
            )

        if self.shell == "cmd":
            return f'cd /d "{cwd}" & {command} & echo {CWD_MARK}%CD%'

        raise ValueError(f"不支持的 shell: {self.shell}")

    # ---------------- 底层执行 ----------------
    def _run_raw(
        self,
        command: str,
        timeout: float = 60,
        on_output: Optional[Callable[[str, str], None]] = None,
    ) -> ExecResult:
        self.connect()
        t0 = time.time()

        with self._lock:
            chan = self.client.get_transport().open_session()
            chan.settimeout(0.5)
            chan.exec_command(command)

            out, err = bytearray(), bytearray()
            deadline = time.time() + timeout
            timed_out = False

            while True:
                while chan.recv_ready():
                    chunk = chan.recv(65536)
                    if not chunk:
                        break
                    if len(out) < MAX_CAPTURE:
                        out += chunk
                    if on_output:
                        on_output("stdout", _decode(chunk))

                while chan.recv_stderr_ready():
                    chunk = chan.recv_stderr(65536)
                    if not chunk:
                        break
                    if len(err) < MAX_CAPTURE:
                        err += chunk
                    if on_output:
                        on_output("stderr", _decode(chunk))

                # 退出码就绪 + 管道已排空 → 结束
                if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                    break

                if time.time() > deadline:
                    timed_out = True
                    try:
                        chan.close()
                    except Exception:
                        pass
                    break

                time.sleep(0.01)

            try:
                code = chan.recv_exit_status()
            except Exception:
                code = -1

        stdout_text = _decode(bytes(out))
        new_cwd = None
        idx = stdout_text.rfind(CWD_MARK)
        if idx != -1:
            tail = stdout_text[idx + len(CWD_MARK):]
            new_cwd = tail.splitlines()[0].strip() if tail.splitlines() else None
            stdout_text = stdout_text[:idx].rstrip("\n")

        return ExecResult(
            exit_code=code,
            stdout=stdout_text,
            stderr=_decode(bytes(err)),
            cwd=new_cwd or "",
            duration=round(time.time() - t0, 3),
            timed_out=timed_out,
        )

    # ---------------- 对外 exec ----------------
    def exec(self, command: str, cwd: Optional[str] = None, timeout: float = 60,
             on_output=None) -> ExecResult:
        use_cwd = cwd or self.cwd or ("/" if self.shell == "bash" else "C:\\")
        r = self._run_raw(self._wrap(command, use_cwd), timeout=timeout, on_output=on_output)
        if r.cwd:
            self.cwd = r.cwd
        return r

    # ---------------- SFTP 文件操作 ----------------
    def sftp(self) -> paramiko.SFTPClient:
        self.connect()
        with self._lock:
            if self._sftp is None:
                self._sftp = self.client.open_sftp()
            return self._sftp

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 500) -> str:
        s = self.sftp()
        lines = []
        with s.open(path, "rb") as f:
            for i, raw in enumerate(f, 1):
                if i < start_line:
                    continue
                if len(lines) >= max_lines:
                    break
                lines.append(_decode(raw).rstrip("\r\n"))
        return "\n".join(lines)

    def read_all(self, path: str) -> str:
        s = self.sftp()
        with s.open(path, "rb") as f:
            return _decode(f.read())

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        s = self.sftp()
        mode = "ab" if append else "wb"
        with s.open(path, mode) as f:
            f.write(content.encode("utf-8"))

    def list_dir(self, path: str) -> list[dict]:
        s = self.sftp()
        items = []
        for attr in s.listdir_attr(path):
            import stat as _stat
            items.append({
                "name": attr.filename,
                "size": attr.st_size,
                "is_dir": _stat.S_ISDIR(attr.st_mode),
                "mtime": attr.st_mtime,
            })
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return items

    def _ensure_remote_dir(self, remote_path: str) -> None:
        import posixpath
        s = self.sftp()
        d = posixpath.dirname(remote_path.replace("\\", "/"))
        if not d:
            return
        parts, cur = [p for p in d.split("/") if p], ""
        for p in parts:
            cur += "/" + p
            try:
                s.stat(cur)
            except IOError:
                try:
                    s.mkdir(cur)
                except IOError:
                    pass

    def upload(self, local: str, remote: str) -> None:
        self._ensure_remote_dir(remote)
        self.sftp().put(local, remote)

    def download(self, remote: str, local: str) -> None:
        import os
        os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
        self.sftp().get(remote, local)