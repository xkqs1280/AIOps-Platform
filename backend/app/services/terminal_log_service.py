"""设备 CLI 会话日志 —— 每次连接自动实时落盘，支持事后下载。

文件命名（华哥 2026-10-21 定）::

    <sysname>-<设备IP>-<YYYYMMDD-HHMM>.log
    例：HX-SW1-192.168.124.62-20261021-1048.log

设计约束与取舍（改动前请先读完，这里每条都是踩过的坑）：

1. **实时**：每段设备输出立刻追加并 ``flush``，浏览器崩溃/断电也不会丢掉已收到的内容。
   所以不能攒够几十 KB 再写。

2. **可读**：网络设备 CLI 会输出大量 ANSI 转义（颜色、清屏、光标定位）和退格擦除序列。
   原样落盘后用记事本打开全是 ``^[[1;32m``、``^H``，等于没记。故落盘前做终端净化：
   剥 ANSI、按行缓冲并真正执行退格。

3. **只记输出，不记输入**：交互式 PTY 下设备会把用户键入回显出来，命令行本身就是日志的
   一部分；而 ``enable`` 密码这类敏感输入设备**不回显**，只记输出天然避免了明文落库落盘。
   ⚠️ 不要"顺手"把前端发来的 input 也写进去 —— 那会把 enable 密码写进明文日志。

4. **行缓冲**：退格要真能擦掉字符就必须持有当前行。按行刷写，长行（>512 字符）先落盘，
   会话结束时兜底刷掉最后一行（可能没有换行符）。

5. **不建后台任务**：留存清理挂在"建立新会话"这一动作上（每小时最多一次），
   避免为一个低频功能常驻一个 asyncio 任务。
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import BACKEND_DIR, settings

logger = logging.getLogger(__name__)

# 平台对外展示/命名统一用北京时间（与 devices.py 导出、notify_service 推送一致）。
# ⚠️ 不能直接用 datetime.now()：.108 等宿主时区是 Etc/UTC，文件名会比华哥的表慢 8 小时。
TZ_8 = timezone(timedelta(hours=8))

#: 单行超过这个长度就先落盘，避免一条超长行（如整段配置）长期占着内存。
_LINE_FLUSH_THRESHOLD = 512

#: Windows 不允许出现的文件名字符 + 全部控制字符。
_ILLEGAL_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")

#: Windows 设备保留名（文件名不能等于这些，否则创建失败）。
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

#: ANSI 转义序列。
#:   CSI  —— 颜色/清屏/光标移动，如 ``\x1b[1;32m``
#:   OSC  —— 设置窗口标题，以 BEL 或 ST 结尾
#:   其余 —— 双字符 ESC 序列
_ESCAPE_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[@-Z\\-_]"
)


def _strip_escape(text: str) -> str:
    """剥掉 ANSI 转义序列，保留可读文本。"""
    return _ESCAPE_RE.sub("", text)


def safe_component(value: str | None, fallback: str = "device", maxlen: int = 80) -> str:
    """把任意设备名/字符串压成安全的文件名片段。

    保留中文（Windows/Linux 都支持），只替换文件系统非法字符与空白。
    """
    text = (value or "").strip()
    text = _ILLEGAL_CHARS_RE.sub("_", text)
    text = _WHITESPACE_RE.sub("_", text)
    # Windows 文件名不能以空格或点结尾
    text = text.strip("._ ")
    if not text:
        return fallback
    if text.upper() in _RESERVED_NAMES:
        text = f"_{text}"
    return text[:maxlen]


def _human_size(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KB"
    return f"{num / 1024 / 1024:.1f} MB"


def _human_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}小时{minutes}分{secs}秒"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


class TerminalSessionLog:
    """单个 CLI 会话的日志文件句柄（实时写、幂等关闭）。"""

    def __init__(
        self,
        path: Path,
        device_name: str,
        ip: str,
        operator: str = "",
        target: str = "",
    ):
        self.path = Path(path)
        self.filename = self.path.name
        self.device_name = device_name
        self.ip = ip
        self.operator = operator
        self.target = target
        self.started_at = datetime.now(TZ_8)
        self._line: list[str] = []
        self._buf: list[str] = []
        self._cr_pending = False
        self._closed = False
        self._byte_count = 0
        self._content_bytes = 0
        self._monotonic_start = time.monotonic()
        self._fh = self.path.open("a", encoding="utf-8", errors="replace", newline="")

        self._write_header()

    # ------------------------------------------------------------------
    # 头部 / 尾部
    # ------------------------------------------------------------------
    def _write_header(self) -> None:
        self._raw(
            "# ============================================================\n"
            "# AIOps 设备 CLI 会话日志\n"
            f"# 设备     : {self.device_name} ({self.ip})\n"
            f"# 操作员   : {self.operator or '-'}\n"
            f"# 连接方式 : {self.target or '-'}\n"
            f"# 开始时间 : {self.started_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
            "# ============================================================\n"
        )
        self._flush()

    def _write_footer(self, reason: str) -> None:
        ended = datetime.now(TZ_8)
        self._raw(
            "# ------------------------------------------------------------\n"
            f"# 结束时间 : {ended.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"# 会话时长 : {_human_duration(time.monotonic() - self._monotonic_start)}\n"
            f"# 日志大小 : {_human_size(self._byte_count)}\n"
            f"# 结束原因 : {reason or '-'}\n"
            "# ------------------------------------------------------------\n"
        )

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def _raw(self, text: str) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(text)
            self._byte_count += len(text.encode("utf-8", "replace"))
        except Exception as exc:  # 磁盘满 / 句柄失效等，绝不能拖垮终端会话
            logger.warning("终端会话日志写入失败 %s: %s", self.path, exc)
            self._fh = None

    def _flush(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.flush()
        except Exception:
            pass

    def _drain(self) -> None:
        """把待写缓冲推到文件（不 flush，由 write() 统一 flush）。"""
        if not self._buf:
            return
        chunk = "".join(self._buf)
        self._buf.clear()
        # 只统计**终端内容**字节（不含头尾），用于判断"这次连接是不是什么都没输出"
        self._content_bytes += len(chunk.encode("utf-8", "replace"))
        self._raw(chunk)

    def _end_line(self) -> None:
        self._buf.append("".join(self._line))
        self._line.clear()
        self._buf.append("\n")
        self._drain()

    def _spill_line(self) -> None:
        """超长行先落盘（不断行，只是把内存里的部分推出去）。"""
        if self._line:
            self._buf.append("".join(self._line))
            self._line.clear()
        self._drain()

    def write(self, data: str) -> None:
        """追加一段设备输出。

        ``data`` 是 :func:`terminal_service._decode_output` 已解码的文本。
        """
        if self._closed or not data:
            return
        text = _strip_escape(data)

        for ch in text:
            # ---- 换行处理 ----
            # ⚠️ 必须先判 \r，再结算换行。
            # 实测（.108 + HX-SW1，2026-10-21，见 build/_probe_ws_raw.py）：H3C Comware
            # 的行尾是 "\r\r\n"（双 CR），不是标准的 "\r\n"。若见到 \r 就立刻结算换行，
            # 每行都会多出一个空行 —— 登录横幅 5 行会被记成 10 行。
            # 规则：连续的 \r 属于同一个"回到行首"，只有遇到非 \r 字符才结算成换行；
            # 于是 "\r\r\n\r\r\n" 仍然得到 2 个换行（设备真实的空行必须保留）。
            if ch == "\r":
                self._cr_pending = True
                continue

            if self._cr_pending:
                self._cr_pending = False
                self._end_line()  # 把 \r 结算成一个换行
                if ch == "\n":
                    continue

            # ---- 退格：真正擦掉当前行最后一个字符（设备用 "\b \b" 擦除）----
            if ch == "\x08":
                if self._line:
                    self._line.pop()
                continue

            if ch == "\n":
                self._end_line()
                continue

            if ch == "\t":
                self._line.append("\t")
            elif ord(ch) < 0x20:
                # BEL、垂直制表等其它控制字符：丢弃
                continue
            else:
                self._line.append(ch)

            if len(self._line) >= _LINE_FLUSH_THRESHOLD:
                self._spill_line()

        self._drain()
        self._flush()

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------
    def close(self, reason: str = "会话结束") -> None:
        """收尾并落盘。幂等。"""
        if self._closed:
            return
        self._closed = True
        try:
            # 兜底刷掉最后一行（通常是光标所在行，没有换行符）
            if self._line:
                self._buf.append("".join(self._line))
                self._line.clear()
                self._buf.append("\n")
            self._cr_pending = False
            self._drain()
            self._write_footer(reason)
            self._flush()
        finally:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None

    @property
    def size(self) -> int:
        return self._byte_count

    def unlink_if_trivial(self, threshold: int = 128) -> bool:
        """连接失败时清理掉几乎没内容的空日志，避免日志目录堆一堆碎片。

        Returns:
            True 表示已删除。
        """
        self.close("连接失败")
        if self._content_bytes > threshold:
            return False
        try:
            self.path.unlink()
            return True
        except Exception:
            return False


class TerminalLogStore:
    """会话日志目录的集中管理：命名、列举、定位、留存清理。"""

    def __init__(self, base_dir: str | Path | None = None):
        if base_dir is None:
            raw = (getattr(settings, "TERMINAL_LOG_DIR", "") or "").strip()
            self.base_dir = Path(raw).expanduser().resolve() if raw else (BACKEND_DIR / "terminal_logs").resolve()
        else:
            self.base_dir = Path(base_dir).expanduser().resolve()
        self.retention_days = int(getattr(settings, "TERMINAL_LOG_RETENTION_DAYS", 30) or 0)
        self._last_purge = 0.0

    # ------------------------------------------------------------------
    @staticmethod
    def _prefix(device_name: str, ip: str) -> str:
        return f"{safe_component(device_name)}-{safe_component(ip, 'ip')}-"

    def _ensure_dir(self) -> Path:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        return self.base_dir

    # ------------------------------------------------------------------
    def create(
        self,
        device_name: str,
        ip: str,
        operator: str = "",
        target: str = "",
    ) -> TerminalSessionLog:
        """为一个新会话创建日志文件（同分钟内重连自动加 ``-2``/``-3`` 后缀）。"""
        directory = self._ensure_dir()
        self.purge_old()

        stem = f"{self._prefix(device_name, ip)}{datetime.now(TZ_8).strftime('%Y%m%d-%H%M')}"
        path = directory / f"{stem}.log"
        n = 2
        while path.exists():
            path = directory / f"{stem}-{n}.log"
            n += 1

        return TerminalSessionLog(
            path,
            device_name=device_name,
            ip=ip,
            operator=operator,
            target=target,
        )

    # ------------------------------------------------------------------
    def list_for(self, device_name: str, ip: str, limit: int = 20) -> list[dict]:
        """列出某台设备的历史会话日志（新 → 旧）。"""
        if not self.base_dir.is_dir():
            return []
        prefix = self._prefix(device_name, ip)
        items: list[dict] = []
        for p in self.base_dir.glob(f"{prefix}*.log"):
            try:
                st = p.stat()
            except OSError:
                continue
            items.append({
                "filename": p.name,
                "size": st.st_size,
                "modified_at": datetime.fromtimestamp(st.st_mtime, TZ_8).strftime("%Y-%m-%d %H:%M:%S"),
            })
        items.sort(key=lambda x: x["modified_at"], reverse=True)
        return items[:limit]

    # ------------------------------------------------------------------
    def resolve(self, device_name: str, ip: str, filename: str) -> Path | None:
        """把下载请求的文件名解析成本机路径。

        三重校验，任一条不过就返回 None：
        1. 必须是纯文件名（无路径分隔符、无 ``..``）—— 防目录穿越；
        2. 必须以本设备的 ``<sysname>-<ip>-`` 开头且以 ``.log`` 结尾 —— 防越权读别的设备；
        3. 解析后的绝对路径必须仍在日志目录内。
        """
        name = (filename or "").strip()
        if not name or "/" in name or "\\" in name or ".." in name:
            return None
        if not name.startswith(self._prefix(device_name, ip)) or not name.endswith(".log"):
            return None

        try:
            base = self.base_dir.resolve()
            candidate = (base / name).resolve()
        except OSError:
            return None
        if candidate.parent != base:
            return None
        return candidate if candidate.is_file() else None

    # ------------------------------------------------------------------
    def purge_old(self) -> int:
        """清理超过留存天数的日志。每小时最多执行一次，返回删除数量。"""
        if self.retention_days <= 0:
            return 0
        now = time.monotonic()
        if now - self._last_purge < 3600:
            return 0
        self._last_purge = now

        if not self.base_dir.is_dir():
            return 0
        cutoff = time.time() - self.retention_days * 86400
        removed = 0
        for p in self.base_dir.glob("*.log"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                continue
        if removed:
            logger.info("终端会话日志清理：删除 %d 个超过 %d 天的文件", removed, self.retention_days)
        return removed


_store: TerminalLogStore | None = None


def get_terminal_log_store() -> TerminalLogStore:
    """进程级单例（目录配置只在启动时读一次）。"""
    global _store
    if _store is None:
        _store = TerminalLogStore()
    return _store
