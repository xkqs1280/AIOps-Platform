#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""remote_agent_mcp.py —— 可直接运行的远程运维小工具（MCP 服务端 + 交互式 CLI）

让 AI 通过 MCP 协议操作 Linux / Windows 主机：跑命令、sudo、读写改远端文件、
双向传文件、增量同步目录树、systemd 运维。核心引擎见同目录 remote_agent_session.py。

用法
----
    # 1) MCP stdio 服务端（给 AI 用；注册进 mcp.json 即可）
    python tools/remote_agent_mcp.py serve

    # 2) 交互式终端（给人用）
    python tools/remote_agent_mcp.py cli 108

    # 3) 一次性执行
    python tools/remote_agent_mcp.py run 108 "systemctl status aiops-backend"
    python tools/remote_agent_mcp.py run 108 --sudo "cat /etc/shadow | wc -l"
    python tools/remote_agent_mcp.py run 108 --json --timeout 120 "df -h"

    # 4) 目录树增量同步（只传真正有差异的文件）
    python tools/remote_agent_mcp.py sync 108 ./backend/app /home/admin1/aiops-platform/backend/app --dry
    python tools/remote_agent_mcp.py sync 108 /var/log/aiops ./logs --pull

    # 5) 主机清单 / 探活 / 自检
    python tools/remote_agent_mcp.py hosts
    python tools/remote_agent_mcp.py probe 108 106
    python tools/remote_agent_mcp.py selftest

主机配置默认读仓库内 `.workbuddy/remote_hosts.json`（已 gitignore，不随仓库外泄），
可用环境变量 `REMOTE_AGENT_CONFIG` 或 `--config` 指定别的路径。

服务端实现约定（照 MCP stdio 传输规范）：
  * stdout 只输出协议 JSON，每行一条，绝不混入任何日志/横幅；
    所有调试输出走 stderr（--debug 打开）。
  * 同时兼容换行分隔 JSON 与 LSP 风格 `Content-Length:` 头两种分帧。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import traceback
from typing import Any, Callable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from remote_agent_session import (  # noqa: E402
    ExecResult,
    HostRegistry,
    RemoteConfigError,
    RemoteError,
    SessionManager,
    __doc__ as _engine_doc,
)

SERVER_NAME = "remote-agent"
SERVER_VERSION = "1.1.0"
DEFAULT_PROTOCOL = "2024-11-05"
DEBUG = bool(os.environ.get("REMOTE_AGENT_DEBUG"))


# --------------------------------------------------------------------------- #
# 基础设施
# --------------------------------------------------------------------------- #
def log(*parts: Any) -> None:
    if not DEBUG:
        return
    sys.stderr.write("[remote-agent] " + " ".join(str(p) for p in parts) + "\n")
    sys.stderr.flush()


def _fmt_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _cast(value: str, typ):
    if typ is None or typ is bool:
        return True
    try:
        return typ(value)
    except (TypeError, ValueError):
        raise SystemExit(f"参数值非法: {value!r}（期望 {typ.__name__}）")


def _consume_flags(tokens: list[str], spec: dict) -> tuple[dict, list[str]]:
    """从 tokens 头部吃掉已知 flag，返回 (解析结果, 剩余 tokens)。

    支持 `--k v`、`--k=v`、布尔开关；遇到 `--` 或首个非 flag 停止。
    """
    out: dict = {}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--":
            i += 1
            break
        if not t.startswith("-"):
            break
        if "=" in t:
            flag, _, val = t.partition("=")
            if flag not in spec:
                break
            key, typ = spec[flag]
            out[key] = _cast(val, typ)
        else:
            if t not in spec:
                break
            key, typ = spec[t]
            if typ is None:
                out[key] = True
            else:
                if i + 1 >= len(tokens):
                    raise SystemExit(f"{t} 需要一个值")
                out[key] = _cast(tokens[i + 1], typ)
                i += 1
        i += 1
    return out, tokens[i:]


# --------------------------------------------------------------------------- #
# 输出渲染
# --------------------------------------------------------------------------- #
def render_exec(r: ExecResult, max_chars: int = 40000) -> str:
    return r.format(max_chars=max_chars)


def render_sync(result: dict) -> str:
    pull = result.get("direction") == "pull"
    lines = [
        f"方向: {'pull 远端→本地' if pull else 'push 本地→远端'}",
        f"本地文件 {result.get('local_count', '?')} 个 / 远端 {result.get('remote_count', '?')} 个",
    ]
    if result.get("dry_run"):
        lines.append("模式: 预演（未实际传输）")
    labels = (("内容不同", "changed"), ("本地缺失", "added")) if pull else \
             (("内容不同", "changed"), ("远端新增", "added"))
    for label, key in labels:
        vals = result.get(key) or []
        if vals:
            lines.append(f"{label} {len(vals)}:")
            lines.extend("  " + v for v in vals[:60])
            if len(vals) > 60:
                lines.append(f"  ... 另有 {len(vals) - 60} 项")
    if not pull and result.get("extra_remote"):
        lines.append(f"远端独有 {len(result['extra_remote'])} 项（默认不删）")
    if result.get("uploaded"):
        lines.append(f"已上传 {len(result['uploaded'])} 个文件")
    if result.get("downloaded"):
        lines.append(f"已下载 {len(result['downloaded'])} 个文件")
    if result.get("deleted"):
        lines.append(f"已删除远端 {len(result['deleted'])} 个文件")
    lines.append(f"内容相同 {len(result.get('unchanged') or [])} 个")
    if result.get("errors"):
        lines.append(f"错误 {len(result['errors'])} 项:")
        lines.extend(f"  {e['file']}: {e['error']}" for e in result["errors"][:20])
    return "\n".join(lines)


def render_hosts(out: dict) -> str:
    lines = [f"配置: {out['config']}   默认: {out['default_host'] or '(未设)'}"]
    for h in out["hosts"]:
        flag = " *" if h["default"] else "  "
        lines.append(f"{flag} {h['name']:<10} {h['username'] or '-'}@"
                     f"{h['target']}:{h['port']}  {h['os']}/{h['shell']}  "
                     f"{h['auth']}  cwd={h['cwd'] or '-'}")
    if len(lines) == 1:
        lines.append("  (未配置任何主机，用 hosts add 添加)")
    return "\n".join(lines)


def render_probe(info: dict) -> str:
    if not info.get("connected"):
        return f"[失败] {info.get('host')} ({info.get('target')}) —— {info.get('error')}"
    head = (f"[OK] {info.get('host')}  {info.get('username')}@{info.get('target')}:"
            f"{info.get('port')}  {info.get('os')}/{info.get('shell')}  "
            f"连接 {info.get('connect_ms')}ms")
    rows = [
        ("主机名", info.get("hostname")),
        ("系统", info.get("distro") or info.get("os_version")),
        ("内核/构建", info.get("kernel")),
        ("运行时长", info.get("uptime") or info.get("boot_time")),
        ("磁盘", info.get("disk")),
        ("登录用户", info.get("login_user")),
        ("sudo", info.get("sudo")),
        ("管理员", "是" if info.get("admin") else ("否" if "admin" in info else None)),
        ("初始 cwd", info.get("cwd")),
    ]
    for k, v in rows:
        if v:
            head += f"\n  {k}: {v}"
    return head


# --------------------------------------------------------------------------- #
# 工具实现（MCP tools/call 与 CLI 共用）
# --------------------------------------------------------------------------- #
class ToolBox:
    def __init__(self, sessions: SessionManager):
        self.sessions = sessions

    # ---- 会话/主机 ----
    def hosts(self, action: str = "list", name: str = "", host: str = "",
              port: int = 22, username: str = "", password: str = "",
              os_type: str = "linux", shell: str = "", cwd: str = "",
              sudo_password: str = "", key_file: str = "",
              set_default: bool = False) -> Any:
        reg = self.sessions.registry
        action = (action or "list").lower()

        if action == "list":
            rows = []
            for n in reg.names():
                spec = reg.hosts[n]
                rows.append({
                    "name": n, "target": spec.get("host"), "port": spec.get("port", 22),
                    "username": spec.get("username"), "os": spec.get("os"),
                    "shell": spec.get("shell"), "cwd": spec.get("cwd"),
                    "auth": "key" if spec.get("key_file") else "password",
                    "default": n == reg.default_host,
                })
            return {"config": reg.path, "default_host": reg.default_host, "hosts": rows}

        if action == "add":
            if not name or not host:
                raise RemoteError("action=add 需要 name 与 host")
            spec = {"host": host, "port": int(port), "os": os_type}
            if username:
                spec["username"] = username
            if password:
                spec["password"] = password
            if shell:
                spec["shell"] = shell
            if cwd:
                spec["cwd"] = cwd
            if sudo_password:
                spec["sudo_password"] = sudo_password
            if key_file:
                spec["key_file"] = key_file
            reg.add(name, spec)
            if set_default:
                reg.default_host = name
            reg.save()
            return {"ok": True, "added": name, "config": reg.path}

        if action == "remove":
            if not name:
                raise RemoteError("action=remove 需要 name")
            ok = reg.remove(name)
            reg.save()
            self.sessions.close(name)
            return {"ok": ok, "removed": name}

        if action == "default":
            if name not in reg.hosts:
                raise RemoteError(f"未知主机: {name}")
            reg.default_host = name
            reg.save()
            return {"ok": True, "default_host": name}

        raise RemoteError("action 支持: list / add / remove / default")

    def session_status(self, host: Optional[str] = None) -> Any:
        live = self.sessions.list()
        reg = self.sessions.registry
        return {"config": reg.path, "default_host": reg.default_host,
                "configured": reg.names(), "live_sessions": live}

    def session_close(self, host: Optional[str] = None) -> Any:
        if host:
            return {"closed": host, "found": self.sessions.close(host)}
        return {"closed_all": self.sessions.close_all()}

    # ---- 命令执行 ----
    def exec(self, command: str, host: Optional[str] = None, cwd: Optional[str] = None,
             timeout: float = 60, sudo: bool = False, pty: bool = False,
             stdin: str = "", env: Optional[dict] = None,
             max_output_chars: int = 40000, raw: bool = False) -> Any:
        if not command or not command.strip():
            raise RemoteError("command 不能为空")
        sess = self.sessions.get(host)
        r = sess.exec(command, cwd=cwd, timeout=float(timeout), sudo=bool(sudo),
                      pty=bool(pty), stdin=stdin or "", env=env)
        if raw:
            return r.as_dict()
        return render_exec(r, max_chars=int(max_output_chars))

    def exec_multi(self, commands: list[str], host: Optional[str] = None,
                   timeout: float = 60, sudo: bool = False,
                   stop_on_error: bool = True, max_output_chars: int = 8000) -> str:
        sess = self.sessions.get(host)
        chunks = []
        for idx, cmd in enumerate(commands, 1):
            r = sess.exec(cmd, timeout=float(timeout), sudo=bool(sudo))
            chunks.append(f"### [{idx}/{len(commands)}] {cmd}\n{render_exec(r, max_output_chars)}")
            if not r.ok and stop_on_error:
                chunks.append("### 已按 stop_on_error 中止")
                break
        return "\n".join(chunks)

    def systemd(self, action: str, unit: str, host: Optional[str] = None,
                lines: int = 80, sudo: bool = True) -> str:
        sess = self.sessions.get(host)
        return render_exec(sess.systemctl(action, unit, lines=int(lines), sudo=bool(sudo)))

    def probe(self, host: Optional[str] = None) -> Any:
        return self.sessions.get(host).probe()

    # ---- 文件 ----
    def read_file(self, path: str, host: Optional[str] = None, start_line: int = 1,
                  max_lines: int = 500, encoding: Optional[str] = None) -> str:
        return self.sessions.get(host).read_file(
            path, start_line=int(start_line), max_lines=int(max_lines), encoding=encoding)

    def write_file(self, path: str, content: str, host: Optional[str] = None,
                   append: bool = False, newline: str = "raw",
                   mode: Optional[str] = None) -> Any:
        return self.sessions.get(host).write_file(
            path, content, append=bool(append), newline=newline, mode=mode)

    def replace_in_file(self, path: str, old: str, new: str,
                        host: Optional[str] = None, count: int = 0) -> Any:
        return self.sessions.get(host).replace_in_file(path, old, new, count=int(count))

    def list_dir(self, path: str = ".", host: Optional[str] = None) -> str:
        items = self.sessions.get(host).list_dir(path)
        lines = [f"{'d' if i['is_dir'] else '-'} {i['mode']:>5} {i['size']:>12}  {i['name']}"
                 for i in items]
        return "\n".join(lines) if lines else "(空目录)"

    def stat_path(self, path: str, host: Optional[str] = None) -> Any:
        return self.sessions.get(host).stat_path(path)

    def upload(self, local_path: str, remote_path: str, host: Optional[str] = None) -> Any:
        return self.sessions.get(host).upload(local_path, remote_path)

    def download(self, remote_path: str, local_path: str, host: Optional[str] = None) -> Any:
        return self.sessions.get(host).download(remote_path, local_path)

    def sync_tree(self, local_dir: str, remote_dir: str, host: Optional[str] = None,
                  direction: str = "push", dry_run: bool = False,
                  include: Optional[list] = None, exclude: Optional[list] = None,
                  delete_extra: bool = False, ignore_noise: bool = True) -> Any:
        sess = self.sessions.get(host)
        direction = (direction or "push").lower()
        if direction in ("push", "up", "upload"):
            res = sess.put_tree(local_dir, remote_dir, include=include, exclude=exclude,
                                dry_run=bool(dry_run), delete_extra=bool(delete_extra),
                                ignore_noise=bool(ignore_noise))
        elif direction in ("pull", "down", "download"):
            res = sess.get_tree(remote_dir, local_dir, include=include, exclude=exclude,
                                dry_run=bool(dry_run), ignore_noise=bool(ignore_noise))
        else:
            raise RemoteError("direction 支持: push / pull")
        res["_summary"] = render_sync(res)
        return res


# --------------------------------------------------------------------------- #
# MCP 工具声明
# --------------------------------------------------------------------------- #
HOST_DESC = "目标主机名（配置里的 key，如 108 / 106）；省略则用 default_host"

TOOLS: list[dict] = [
    {
        "name": "hosts",
        "description": "查看/增删主机登记表（配置默认在仓库 .workbuddy/remote_hosts.json）。"
                       "action=list 返回所有主机；add 新增或覆盖（需 name+host）；"
                       "remove 删除；default 设默认主机。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "add", "remove", "default"],
                           "default": "list"},
                "name": {"type": "string", "description": "主机别名（配置 key）"},
                "host": {"type": "string", "description": "IP 或域名"},
                "port": {"type": "integer", "default": 22},
                "username": {"type": "string"},
                "password": {"type": "string"},
                "os_type": {"type": "string", "enum": ["linux", "windows"], "default": "linux"},
                "shell": {"type": "string", "description": "bash/powershell/cmd，留空按 os_type 推断"},
                "cwd": {"type": "string", "description": "登录后默认工作目录"},
                "sudo_password": {"type": "string", "description": "sudo 口令，留空则复用 password"},
                "key_file": {"type": "string", "description": "私钥路径（本地）"},
                "set_default": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "exec",
        "description": "在远端执行一条命令并返回退出码/stdout/stderr/新 cwd。"
                       "会话保持 cwd（cd 会记住）。sudo=true 时用 `sudo -S` 自动喂口令提权。"
                       "适合 shell 命令、docker/systemctl、脚本调用等一切操作。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令（可多行）"},
                "host": {"type": "string", "description": HOST_DESC},
                "cwd": {"type": "string", "description": "本条命令的工作目录（会更新会话 cwd）"},
                "timeout": {"type": "number", "default": 60, "description": "秒；超时会强杀远端进程"},
                "sudo": {"type": "boolean", "default": False},
                "pty": {"type": "boolean", "default": False, "description": "强制分配伪终端"},
                "stdin": {"type": "string", "description": "喂给命令的标准输入"},
                "env": {"type": "object", "description": "附加环境变量"},
                "max_output_chars": {"type": "integer", "default": 40000},
                "raw": {"type": "boolean", "default": False,
                        "description": "true 返回结构化字段而非渲染文本"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "exec_multi",
        "description": "在同一会话里按顺序执行多条命令（省往返）。默认遇错中止。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "commands": {"type": "array", "items": {"type": "string"}},
                "host": {"type": "string", "description": HOST_DESC},
                "timeout": {"type": "number", "default": 60},
                "sudo": {"type": "boolean", "default": False},
                "stop_on_error": {"type": "boolean", "default": True},
            },
            "required": ["commands"],
        },
    },
    {
        "name": "read_file",
        "description": "读远端文本文件（自动探测 utf-8/gbk）。start_line 支持负数表示从尾部数，"
                       "例如 start_line=-200 读最后 200 行。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "host": {"type": "string", "description": HOST_DESC},
                "start_line": {"type": "integer", "default": 1},
                "max_lines": {"type": "integer", "default": 500,
                              "description": "<=0 表示不限制"},
                "encoding": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "写远端文本文件（自动建父目录）。newline 强烈建议对 .sh/.py/.conf 用 lf，"
                       "否则在 Windows 主机上写出的脚本会报 `$'\\r': command not found`。"
                       "mode 如 \"755\" 可在写后设权限。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "host": {"type": "string", "description": HOST_DESC},
                "append": {"type": "boolean", "default": False},
                "newline": {"type": "string", "enum": ["raw", "lf", "crlf"], "default": "raw"},
                "mode": {"type": "string", "description": "八进制权限，如 755"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "replace_in_file",
        "description": "在远端文件里做精确字符串替换（写临时文件后原子改名）。"
                       "返回命中数。count=0 表示全部替换。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
                "host": {"type": "string", "description": HOST_DESC},
                "count": {"type": "integer", "default": 0},
            },
            "required": ["path", "old", "new"],
        },
    },
    {
        "name": "list_dir",
        "description": "列远端目录（目录在前，含八进制权限/大小）",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "default": "."},
                           "host": {"type": "string", "description": HOST_DESC}},
        },
    },
    {
        "name": "stat_path",
        "description": "查看远端路径的大小/权限/时间",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"},
                           "host": {"type": "string", "description": HOST_DESC}},
            "required": ["path"],
        },
    },
    {
        "name": "upload",
        "description": "上传单个文件到远端（自动建父目录、保留 mtime）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "local_path": {"type": "string"},
                "remote_path": {"type": "string"},
                "host": {"type": "string", "description": HOST_DESC},
            },
            "required": ["local_path", "remote_path"],
        },
    },
    {
        "name": "download",
        "description": "从远端下载单个文件到本地（自动建父目录、保留 mtime）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "remote_path": {"type": "string"},
                "local_path": {"type": "string"},
                "host": {"type": "string", "description": HOST_DESC},
            },
            "required": ["remote_path", "local_path"],
        },
    },
    {
        "name": "sync_tree",
        "description": "目录树增量同步：按 md5 比对（容忍 CRLF/LF 差异），只传真正不同的文件。"
                       "direction=push 本地→远端 / pull 远端→本地；dry_run 只报差异不传。"
                       "默认不删远端多余文件（delete_extra 显式开启）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "local_dir": {"type": "string"},
                "remote_dir": {"type": "string"},
                "host": {"type": "string", "description": HOST_DESC},
                "direction": {"type": "string", "enum": ["push", "pull"], "default": "push"},
                "dry_run": {"type": "boolean", "default": False},
                "include": {"type": "array", "items": {"type": "string"},
                            "description": "正则白名单（相对路径）"},
                "exclude": {"type": "array", "items": {"type": "string"},
                            "description": "正则黑名单（相对路径）"},
                "delete_extra": {"type": "boolean", "default": False,
                                 "description": "仅 push 且显式开启时删除远端多余文件"},
                "ignore_noise": {"type": "boolean", "default": True,
                                 "description": "跳过 __pycache__/.git/node_modules 等缓存目录"},
            },
            "required": ["local_dir", "remote_dir"],
        },
    },
    {
        "name": "probe",
        "description": "主机探活与体检：连通性、主机名、发行版、uptime、磁盘、登录用户、"
                       "sudo 是否免密",
        "inputSchema": {
            "type": "object",
            "properties": {"host": {"type": "string", "description": HOST_DESC}},
        },
    },
    {
        "name": "systemd",
        "description": "systemd 快捷运维（仅 Linux）：status / journal / is-active / "
                       "start / stop / restart / reload / enable / disable / daemon-reload",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "unit": {"type": "string", "description": "如 aiops-backend"},
                "host": {"type": "string", "description": HOST_DESC},
                "lines": {"type": "integer", "default": 80, "description": "status/journal 行数"},
                "sudo": {"type": "boolean", "default": True},
            },
            "required": ["action", "unit"],
        },
    },
    {
        "name": "session",
        "description": "会话管理：status 看已连接的会话与配置；close 关闭某主机（或全部）连接",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "close"], "default": "status"},
                "host": {"type": "string", "description": HOST_DESC + "；close 时省略=全部"},
            },
        },
    },
]


def build_dispatch(toolbox: ToolBox) -> dict[str, Callable[[dict], Any]]:
    tb = toolbox
    return {
        "hosts": lambda a: tb.hosts(**a),
        "exec": lambda a: tb.exec(**a),
        "exec_multi": lambda a: tb.exec_multi(**a),
        "read_file": lambda a: tb.read_file(**a),
        "write_file": lambda a: tb.write_file(**a),
        "replace_in_file": lambda a: tb.replace_in_file(**a),
        "list_dir": lambda a: tb.list_dir(**a),
        "stat_path": lambda a: tb.stat_path(**a),
        "upload": lambda a: tb.upload(**a),
        "download": lambda a: tb.download(**a),
        "sync_tree": lambda a: tb.sync_tree(**a),
        "probe": lambda a: tb.probe(**a),
        "systemd": lambda a: tb.systemd(**a),
        "session": lambda a: (tb.session_close(a.get("host")) if a.get("action") == "close"
                              else tb.session_status(a.get("host"))),
    }


def _normalize_args(name: str, args: dict) -> dict:
    """把 MCP/CLI 传来的参数名映射到 ToolBox 形参（host 通用；os → os_type）。"""
    args = dict(args or {})
    if name == "hosts" and "os" in args:
        args["os_type"] = args.pop("os")
    args.pop("_", None)
    return args


# --------------------------------------------------------------------------- #
# MCP stdio 服务端
# --------------------------------------------------------------------------- #
class McpServer:
    def __init__(self, sessions: SessionManager, toolbox: ToolBox):
        self.sessions = sessions
        self.toolbox = toolbox
        self.dispatch = build_dispatch(toolbox)

    # ---------------- 传输层 ----------------
    def _read_messages(self):
        """逐条读入协议消息，兼容换行 JSON 与 Content-Length 分帧。"""
        stream = sys.stdin.buffer
        while True:
            line = stream.readline()
            if not line:
                return
            text = line.strip()
            if not text:
                continue
            if text.lower().startswith(b"content-length:"):
                try:
                    length = int(text.split(b":", 1)[1].strip())
                except ValueError:
                    log("坏 Content-Length 头:", text[:80])
                    continue
                while True:  # 吃掉头部剩余行
                    hdr = stream.readline()
                    if not hdr or hdr.strip() == b"":
                        break
                payload = stream.read(length)
                if not payload:
                    return
                yield payload.decode("utf-8", errors="replace")
                continue
            yield text.decode("utf-8", errors="replace")

    def _send(self, obj: dict) -> None:
        """只往 stdout 写协议 JSON。

        直接操作底层 buffer 并强制 utf-8：不要用 io.TextIOWrapper 包 sys.stdout.buffer
        —— 包装对象被 GC 时会连带关闭 stdout，把后续所有输出吃掉（自检里真踩过）。
        """
        payload = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
        raw = payload.encode("utf-8")
        buf = getattr(sys.stdout, "buffer", None)
        if buf is None:  # 宿主替换过 stdout（测试/嵌入场景）
            sys.stdout.write(payload)
            sys.stdout.flush()
            return
        try:
            buf.write(raw)
            buf.flush()
        except (BrokenPipeError, ValueError):
            raise SystemExit(0)

    def _reply(self, msg_id, result=None, error=None) -> None:
        msg = {"jsonrpc": "2.0", "id": msg_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        self._send(msg)

    # ---------------- 协议层 ----------------
    def handle(self, msg: dict) -> Optional[dict]:
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}
        is_notification = "id" not in msg

        if method == "initialize":
            client_proto = params.get("protocolVersion") or DEFAULT_PROTOCOL
            reg = self.sessions.registry
            return self._result(msg_id, {
                "protocolVersion": client_proto,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "远程运维工具：用 exec 跑命令（会话保留 cwd）、read_file/write_file/"
                    "replace_in_file 读写改远端文件、upload/download 传文件、"
                    "sync_tree 做目录树增量同步、probe 体检、systemd 管服务。"
                    f"主机配置：{reg.path}；已配置主机：{', '.join(reg.names()) or '(空)'}；"
                    f"默认主机：{reg.default_host or '(未设)'}。"
                    "命令失败（非零退出码）不算工具错误，请读 exit_code 与 stderr 判断。"
                ),
            })

        if method in ("notifications/initialized", "initialized",
                      "notifications/cancelled", "notifications/roots/list_changed",
                      "notifications/progress", "logging/setLevel"):
            return None

        if method == "ping":
            return self._result(msg_id, {})

        if method == "tools/list":
            return self._result(msg_id, {"tools": TOOLS})

        if method == "tools/call":
            name = params.get("name")
            args = _normalize_args(name or "", params.get("arguments") or {})
            if name not in self.dispatch:
                return self._reply(msg_id, error={
                    "code": -32602, "message": f"未知工具: {name}",
                    "data": {"available": sorted(self.dispatch)},
                })
            return self._reply(msg_id, result=self._call_tool(name, args))

        if method in ("resources/list", "prompts/list"):
            return self._result(msg_id, {"resources": []} if method.startswith("resources")
                                else {"prompts": []})

        if is_notification:
            log("忽略未知通知:", method)
            return None
        return self._reply(msg_id, error={"code": -32601, "message": f"未实现的方法: {method}"})

    def _result(self, msg_id, result) -> Optional[dict]:
        if msg_id is None:
            return None
        self._reply(msg_id, result=result)
        return None

    def _call_tool(self, name: str, args: dict) -> dict:
        try:
            out = self.dispatch[name](args)
        except TypeError as exc:
            return {"content": [{"type": "text", "text": f"参数不合法: {exc}"}],
                    "isError": True}
        except (RemoteError, RemoteConfigError) as exc:
            return {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True}
        except Exception as exc:  # noqa: BLE001
            log("工具异常:", name, traceback.format_exc())
            return {"content": [{"type": "text",
                                 "text": f"内部错误: {type(exc).__name__}: {exc}"}],
                    "isError": True}
        text = out if isinstance(out, str) else _fmt_json(out)
        return {"content": [{"type": "text", "text": text}]}

    def serve(self) -> int:
        try:  # Windows 上默认可能是 gbk/cp936，协议必须是 utf-8
            sys.stdout.reconfigure(encoding="utf-8", newline="\n")
            sys.stdin.reconfigure(encoding="utf-8")
        except Exception:
            pass
        log(f"启动 MCP 服务端 {SERVER_NAME} v{SERVER_VERSION}，"
            f"配置 {self.sessions.registry.path}，主机 {self.sessions.registry.names()}")
        try:
            for raw in self._read_messages():
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as exc:
                    self._send({"jsonrpc": "2.0", "id": None,
                                "error": {"code": -32700, "message": f"JSON 解析失败: {exc}"}})
                    continue
                if isinstance(msg, list):
                    for one in msg:
                        self.handle(one)
                    continue
                try:
                    self.handle(msg)
                except Exception as exc:  # noqa: BLE001
                    log("处理消息异常:", traceback.format_exc())
                    if "id" in msg:
                        self._reply(msg["id"], error={
                            "code": -32603, "message": f"内部错误: {exc}"})
        except (BrokenPipeError, KeyboardInterrupt):
            pass
        finally:
            self.sessions.close_all()
        return 0


# --------------------------------------------------------------------------- #
# 交互式 CLI
# --------------------------------------------------------------------------- #
HELP_TEXT = """\
远程运维交互终端 —— 直接输入命令回车即在远端执行；以 ':' 开头的是本地指令。

  :help                显示本帮助
  :hosts               列出已配置主机            :use <主机>      切换目标主机
  :cd <目录>           切换远端工作目录          :pwd             显示远端 cwd
  :sudo <命令>         以 sudo 执行（自动喂口令） :pty <命令>      分配伪终端执行
  :ls [目录]           列远端目录                :cat <文件>      打印远端文件
  :tail <文件> [n]     看文件末尾                :put <本地> [远端] 上传
  :get <远端> [本地]   下载                      :sync <本地> <远端> 目录树增量同步(预演)
  :sys                 主机体检                  :timeout <秒>    设置默认超时
  :json on|off         输出原始 JSON             :local <命令>    在本机执行命令
  :exit / :q           退出

提示：普通行直接用本地 shell 的引号规则即可（命令原样发给远端）。"""


class Interactor:
    def __init__(self, toolbox: ToolBox, sessions: SessionManager):
        self.tb = toolbox
        self.sessions = sessions
        self.current: Optional[str] = None
        self.timeout = 60.0
        self.json_mode = False
        self.stream = sys.stdout.isatty()

    # ---- 提示符 ----
    def prompt(self) -> str:
        host = self.current or self.sessions.registry.default_host or "?"
        cwd = ""
        try:
            cwd = self.sessions.get(host).cwd or ""
        except Exception:
            pass
        return f"\033[36mra:{host}\033[0m {cwd}> " if sys.stdout.isatty() else f"ra:{host} {cwd}> "

    # ---- 辅助 ----
    def _emit(self, obj, renderer=None) -> None:
        """json 模式一律输出结构化 JSON；否则优先用 renderer 渲染成人类可读文本。"""
        if isinstance(obj, str):
            print(obj)
        elif self.json_mode:
            print(_fmt_json(obj))
        elif renderer is not None:
            print(renderer(obj))
        else:
            print(_fmt_json(obj))

    def _out_stream(self, kind: str, chunk: str) -> None:
        target = sys.stderr if kind == "stderr" else sys.stdout
        target.write(chunk)
        target.flush()

    def run_exec(self, command: str, sudo: bool = False, pty: bool = False) -> None:
        sess = self.sessions.get(self.current)
        r = sess.exec(command, timeout=self.timeout, sudo=sudo, pty=pty,
                      on_output=self._out_stream if self.stream else None)
        if self.json_mode:
            print(_fmt_json(r.as_dict()))
            return
        if self.stream:
            print()          # 流式输出完毕后补一个换行，再接状态行
        else:
            # 非 tty（被管道/重定向）时没有流式回调，必须显式回显正文
            if r.stdout:
                sys.stdout.write(r.stdout if r.stdout.endswith("\n") else r.stdout + "\n")
            if r.stderr:
                sys.stderr.write(r.stderr if r.stderr.endswith("\n") else r.stderr + "\n")
            if not r.stdout and not r.stderr:
                print("(无输出)")
        tail = f"[exit={r.exit_code}] {r.duration}s cwd={r.cwd}"
        if r.timed_out:
            tail += "  ⚠ 超时（已强杀远端进程）"
        if r.truncated:
            tail += "  ⚠ 输出被截断"
        print(("\033[32m" if r.exit_code == 0 else "\033[31m") + tail + "\033[0m")

    # ---- 主循环 ----
    def loop(self) -> int:
        try:
            self.current = self.current or self.sessions.registry.default_host
        except Exception:
            pass
        print(f"remote-agent 交互终端 | 配置 {self.sessions.registry.path}")
        print(f"主机: {', '.join(self.sessions.registry.names()) or '(空)'}"
              f" | 默认: {self.sessions.registry.default_host or '(未设)'}")
        print("输入 :help 看指令，:exit 退出\n")

        while True:
            try:
                line = input(self.prompt())
            except (EOFError, KeyboardInterrupt):
                print()
                break
            line = line.rstrip("\n")
            if not line.strip():
                continue

            if not line.startswith(":"):
                try:
                    self.run_exec(line)
                except Exception as exc:  # noqa: BLE001
                    print(f"\033[31m错误: {exc}\033[0m", file=sys.stderr)
                continue

            try:
                if not self._local_command(line):
                    break
            except Exception as exc:  # noqa: BLE001
                print(f"\033[31m错误: {exc}\033[0m", file=sys.stderr)
        self.sessions.close_all()
        return 0

    def _local_command(self, line: str) -> bool:
        cmd, _, rest = line[1:].partition(" ")
        cmd = cmd.strip().lower()
        rest = rest.strip()

        if cmd in ("exit", "q", "quit"):
            return False
        if cmd in ("help", "h", "?"):
            print(HELP_TEXT)
        elif cmd == "hosts":
            self._emit(self.tb.hosts(action="list"), render_hosts)
        elif cmd == "use":
            name = rest or self.sessions.registry.default_host
            if name not in self.sessions.registry.hosts:
                print(f"未知主机: {name}")
            else:
                self.current = name
                print(f"已切换到 {name}（{self.sessions.registry.hosts[name].get('host')}）")
        elif cmd == "cd":
            if not rest:
                print(self.sessions.get(self.current).cwd)
            else:
                self.sessions.get(self.current).cwd = rest
                print(f"远端 cwd → {rest}")
        elif cmd == "pwd":
            print(self.sessions.get(self.current).cwd)
        elif cmd == "sudo":
            self.run_exec(rest, sudo=True)
        elif cmd == "pty":
            self.run_exec(rest, pty=True)
        elif cmd in ("ls", "ll"):
            print(self.tb.list_dir(rest or ".", host=self.current))
        elif cmd == "cat":
            print(self.tb.read_file(rest, host=self.current, max_lines=0))
        elif cmd == "tail":
            parts = rest.split()
            path = parts[0] if parts else ""
            n = int(parts[1]) if len(parts) > 1 else 200
            self._emit(self.tb.read_file(path, host=self.current,
                                         start_line=-n, max_lines=n))
        elif cmd == "put":
            parts = rest.split()
            if not parts:
                print("用法: :put <本地路径> [远端路径]")
            else:
                remote = parts[1] if len(parts) > 1 else \
                    f"{self.sessions.get(self.current).cwd}/{os.path.basename(parts[0])}"
                self._emit(self.tb.upload(parts[0], remote, host=self.current))
        elif cmd == "get":
            parts = rest.split()
            if len(parts) < 2:
                print("用法: :get <远端路径> <本地路径>")
            else:
                self._emit(self.tb.download(parts[0], parts[1], host=self.current))
        elif cmd == "sync":
            parts = rest.split()
            if len(parts) < 2:
                print("用法: :sync <本地目录> <远端目录> [--real]（默认预演）")
            else:
                real = "--real" in parts
                parts = [p for p in parts if not p.startswith("--")]
                self._emit(render_sync(self.tb.sync_tree(
                    parts[0], parts[1], host=self.current, dry_run=not real)))
        elif cmd in ("sys", "probe"):
            self._emit(self.tb.probe(self.current), render_probe)
        elif cmd == "timeout":
            self.timeout = float(rest or 60)
            print(f"默认超时 → {self.timeout}s")
        elif cmd == "json":
            self.json_mode = rest.lower() in ("on", "1", "true", "yes")
            print(f"JSON 模式: {'开' if self.json_mode else '关'}")
        elif cmd == "local":
            import subprocess
            subprocess.run(rest, shell=True)
        elif cmd in ("close", "disconnect"):
            self._emit(self.tb.session_close(self.current or rest or None))
        else:
            print(f"未知指令 :{cmd}，输入 :help 查看")
        return True


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="remote_agent_mcp.py",
        description="远程运维小工具：MCP 服务端 + 交互式 CLI（Linux / Windows）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP_TEXT,
    )
    p.add_argument("--config", "-c", help="主机配置文件路径（默认 .workbuddy/remote_hosts.json）")
    p.add_argument("--debug", action="store_true", help="把调试日志打到 stderr")
    sub = p.add_subparsers(dest="mode")

    sub.add_parser("serve", help="以 MCP stdio 服务端运行（默认）")
    p_cli = sub.add_parser("cli", help="交互式终端")
    p_cli.add_argument("host", nargs="?", help="目标主机名")

    p_run = sub.add_parser("run", help="一次性执行命令（flag 放在 host 之后、命令之前）")
    p_run.add_argument("host")
    p_run.add_argument("command", nargs=argparse.REMAINDER,
                       help="--sudo --pty --json --cwd DIR --timeout N -- 命令")

    p_sync = sub.add_parser("sync", help="目录树增量同步")
    p_sync.add_argument("host")
    p_sync.add_argument("local")
    p_sync.add_argument("remote")
    p_sync.add_argument("--pull", action="store_true", help="远端→本地（默认本地→远端）")
    p_sync.add_argument("--dry", action="store_true", help="只报差异不传输")
    p_sync.add_argument("--delete", action="store_true", help="删除远端多余文件（危险）")
    p_sync.add_argument("--include", action="append", default=None, help="正则白名单，可重复")
    p_sync.add_argument("--exclude", action="append", default=None, help="正则黑名单，可重复")
    p_sync.add_argument("--json", action="store_true")

    p_hosts = sub.add_parser("hosts", help="主机登记表管理")
    p_hosts.add_argument("action", nargs="?", default="list",
                         choices=["list", "add", "remove", "default"])
    p_hosts.add_argument("name", nargs="?")
    p_hosts.add_argument("--host")
    p_hosts.add_argument("--port", type=int, default=22)
    p_hosts.add_argument("--user", dest="username")
    p_hosts.add_argument("--password")
    p_hosts.add_argument("--os", dest="os_type", default="linux")
    p_hosts.add_argument("--shell")
    p_hosts.add_argument("--cwd")
    p_hosts.add_argument("--sudo-password")
    p_hosts.add_argument("--key-file")
    p_hosts.add_argument("--default", action="store_true")
    p_hosts.add_argument("--json", action="store_true")

    p_probe = sub.add_parser("probe", help="主机探活体检")
    p_probe.add_argument("host", nargs="*")
    p_probe.add_argument("--json", action="store_true")

    sub.add_parser("selftest", help="离线自检（不联网）")
    return p


RUN_FLAGS = {
    "--sudo": ("sudo", None),
    "--pty": ("pty", None),
    "--json": ("json", None),
    "--raw": ("raw", None),
    "--cwd": ("cwd", str),
    "--timeout": ("timeout", float),
    "--stdin": ("stdin", str),
}


def main(argv: Optional[list[str]] = None) -> int:
    global DEBUG
    args = build_parser().parse_args(argv)
    if args.debug:
        DEBUG = True

    sessions = SessionManager(HostRegistry(args.config))
    toolbox = ToolBox(sessions)
    mode = args.mode or "serve"

    if mode == "selftest":
        return run_selftest()

    if mode == "serve":
        return McpServer(sessions, toolbox).serve()

    if mode == "cli":
        it = Interactor(toolbox, sessions)
        it.current = args.host
        return it.loop()

    if mode == "run":
        flags, command = _consume_flags(list(args.command), RUN_FLAGS)
        command = [c for c in command if c != "--"]
        if not command:
            print("用法: run <host> [--sudo|--pty|--json|--cwd DIR|--timeout N] <命令>",
                  file=sys.stderr)
            return 2
        cmd = " ".join(command)
        r = sessions.get(args.host).exec(
            cmd,
            cwd=flags.get("cwd"),
            timeout=float(flags.get("timeout", 60)),
            sudo=bool(flags.get("sudo")),
            pty=bool(flags.get("pty")),
            stdin=flags.get("stdin", ""),
            on_output=(lambda k, c: sys.stdout.write(c) if k == "stdout"
                       else sys.stderr.write(c)) if sys.stdout.isatty() else None,
        )
        if flags.get("json") or flags.get("raw"):
            print(_fmt_json(r.as_dict()))
        else:
            if sys.stdout.isatty():
                print()
            print(render_exec(r))
        sessions.close_all()
        return r.exit_code if not r.timed_out else 124

    if mode == "sync":
        direction = "pull" if args.pull else "push"
        res = toolbox.sync_tree(args.local, args.remote, host=args.host,
                                direction=direction, dry_run=args.dry,
                                include=args.include, exclude=args.exclude,
                                delete_extra=args.delete)
        print(_fmt_json(res) if args.json else render_sync(res))
        sessions.close_all()
        return 0 if not res["errors"] else 1

    if mode == "hosts":
        if args.action == "list":
            out = toolbox.hosts(action="list")
            print(_fmt_json(out) if args.json else render_hosts(out))
            return 0
        if args.action == "add":
            out = toolbox.hosts(action="add", name=args.name, host=args.host,
                                port=args.port, username=args.username,
                                password=args.password, os_type=args.os_type,
                                shell=args.shell or "", cwd=args.cwd or "",
                                sudo_password=args.sudo_password or "",
                                key_file=args.key_file or "",
                                set_default=args.default)
        elif args.action == "remove":
            out = toolbox.hosts(action="remove", name=args.name)
        else:
            out = toolbox.hosts(action="default", name=args.name)
        print(_fmt_json(out) if args.json else f"已更新配置: {out}")
        return 0

    if mode == "probe":
        targets = args.host or sessions.registry.names()
        if not targets:
            print("没有可探测的主机，请先 hosts add", file=sys.stderr)
            return 2
        failed = 0
        results = []
        for t in targets:
            try:
                info = toolbox.probe(t)
            except Exception as exc:  # noqa: BLE001
                info = {"host": t, "connected": False, "error": str(exc)}
            results.append(info)
            if not info.get("connected"):
                failed += 1
            if not args.json:
                print(render_probe(info))
                print()
        if args.json:
            print(_fmt_json(results))
        sessions.close_all()
        return 1 if failed else 0

    return 0


# --------------------------------------------------------------------------- #
# 离线自检
# --------------------------------------------------------------------------- #
def run_selftest() -> int:
    from remote_agent_session import (SSHSession, decode_bytes, _hashes_same,
                                      _md5, _sniff_encoding)
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append((name, bool(cond), detail))

    # 1) 编码探测：跨分片 UTF-8 / GBK
    zh = "中文输出测试"
    raw = zh.encode("utf-8")
    check("utf-8 整段解码", decode_bytes(raw) == zh)
    check("gbk 整段解码", decode_bytes(zh.encode("gbk")) == zh)
    check("跨分片不截断多字节",
          decode_bytes(raw[:4] + raw[4:]) == zh and _sniff_encoding(raw) == "utf-8")
    check("二进制兜底不抛异常", isinstance(decode_bytes(b"\xff\xfe\x00\x01"), str))

    # 2) LF 容差比对：远端存 LF，本地文件是 CRLF
    crlf, lf = _md5(b"line1\r\nline2\r\n"), _md5(b"line1\nline2\n")
    check("LF 容差判定相同", _hashes_same(lf, crlf, lf, True))
    check("关闭容差后判为不同", not _hashes_same(lf, crlf, lf, False))
    check("内容真不同仍能识别", not _hashes_same(_md5(b"other\n"), crlf, lf, True))

    # 3) shell 包装：bash 不能出现 `;` 独占一行（语法错误）
    sess = SSHSession("selftest", {"host": "127.0.0.1", "username": "u",
                                   "password": "p", "os": "linux", "shell": "bash"})
    wrapped = sess._wrap("echo hi", "/tmp", "__MK__")
    check("bash 包装含哨兵", "__MK__" in wrapped and "echo hi" in wrapped)
    bad = [ln for ln in wrapped.splitlines() if ln.strip() == ";"]
    check("bash 包装无孤立分号", not bad, str(bad))
    check("bash 包装保留退出码", "exit $__ra_rc" in wrapped)
    check("bash 包装 cwd 转义", sess._wrap("x", "/a b'c", "M").count("'/a b'\\''c'") == 1)

    ps = SSHSession("selftest-ps", {"host": "h", "username": "u", "password": "p",
                                    "os": "windows", "shell": "powershell"})
    pw = ps._wrap("Get-Date", "C:\\tmp", "MK")
    check("powershell 包装含哨兵", "MK" in pw and "Set-Location" in pw)
    check("powershell 包装重置 LASTEXITCODE", "$global:LASTEXITCODE=$null" in pw)

    cmd_sess = SSHSession("selftest-cmd", {"host": "h", "username": "u", "password": "p",
                                           "os": "windows", "shell": "cmd"})
    cw = cmd_sess._wrap("dir", "C:\\x", "MK")
    check("cmd 包装开延迟展开", "EnableDelayedExpansion" in cw and "exit /b !__ra_rc!" in cw)

    # 4) 哨兵每次唯一
    check("哨兵每次唯一", sess._new_marker() != sess._new_marker())

    # 5) shell 引号
    from remote_agent_session import _shell_quote
    check("单引号转义", _shell_quote("a'b") == "'a'\\''b'")

    # 6) 主机解析
    reg = HostRegistry(path=os.path.join(os.path.dirname(_HERE), ".workbuddy",
                                         "_selftest_hosts.json"), autoload=False)
    reg.add("t1", {"host": "10.0.0.1", "username": "u", "password": "p"})
    check("主机默认 shell 推断", reg.hosts["t1"]["shell"] == "bash")
    check("内联 user@host:port 解析",
          reg.resolve("root@1.2.3.4:2222")[1]["port"] == 2222)
    check("裸 host 解析", reg.resolve("1.2.3.4")[1]["host"] == "1.2.3.4")
    reg.add("w1", {"host": "10.0.0.2", "username": "u", "password": "p", "os": "windows"})
    check("windows 默认 shell", reg.hosts["w1"]["shell"] == "powershell")
    try:
        reg.resolve("不存在的鬼名字!!")
        check("未知主机报错", False)
    except Exception:
        check("未知主机报错", True)

    # 7) 工具声明自洽
    names = [t["name"] for t in TOOLS]
    check("工具名唯一", len(names) == len(set(names)), f"{len(names)} 个")
    check("每个工具都有 inputSchema",
          all(isinstance(t.get("inputSchema"), dict) for t in TOOLS))
    disp = build_dispatch(ToolBox(SessionManager(HostRegistry(path="x", autoload=False))))
    check("工具声明与实现一一对应", set(names) == set(disp), )

    # 8) MCP 报文往返
    srv = McpServer(SessionManager(HostRegistry(path="x", autoload=False)),
                    ToolBox(SessionManager(HostRegistry(path="x", autoload=False))))
    captured: list[dict] = []
    srv._send = lambda obj: captured.append(obj)  # type: ignore[assignment]
    srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
    init = captured[-1]["result"]
    check("initialize 回显协议版本", init["protocolVersion"] == "2025-06-18")
    check("initialize 声明 tools 能力", "tools" in init["capabilities"])
    srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    check("initialized 通知不回包", len(captured) == 1)
    srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    check("tools/list 返回工具", len(captured[-1]["result"]["tools"]) == len(TOOLS))
    srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "no_such_tool", "arguments": {}}})
    check("未知工具返回 -32602", captured[-1]["error"]["code"] == -32602)
    srv.handle({"jsonrpc": "2.0", "id": 4, "method": "ping"})
    check("ping 有响应", captured[-1]["result"] == {})
    srv.handle({"jsonrpc": "2.0", "id": 5, "method": "no/such/method"})
    check("未知方法返回 -32601", captured[-1]["error"]["code"] == -32601)

    # 9) 参数名归一化
    check("os → os_type 归一化", _normalize_args("hosts", {"os": "windows"})["os_type"] == "windows")

    # 10) ExecResult 渲染
    r = ExecResult(exit_code=1, stdout="o", stderr="e", cwd="/x", duration=0.5,
                   host="108", command="ls")
    text = r.format()
    check("结果含退出码与分节", "[exit=1]" in text and "---- stderr ----" in text)
    check("ok 属性正确", r.ok is False and ExecResult(exit_code=0).ok is True)
    long_r = ExecResult(exit_code=0, stdout="x" * 90000)
    check("超长输出被截断", "已截断" in long_r.format(max_chars=40000))

    # 11) 传输层分帧（Content-Length + 换行 JSON）
    lsp_payload = b'{"jsonrpc":"2.0","id":9,"method":"ping"}'
    buf = (b"Content-Length: " + str(len(lsp_payload)).encode() + b"\r\n\r\n"
           + lsp_payload + b"\n"
           b'{"jsonrpc":"2.0","id":10,"method":"ping"}\n')
    old_stdin = sys.stdin
    try:
        sys.stdin = type("S", (), {"buffer": io.BytesIO(buf)})()  # type: ignore
        srv2 = McpServer(SessionManager(HostRegistry(path="x", autoload=False)),
                         ToolBox(SessionManager(HostRegistry(path="x", autoload=False))))
        msgs = list(srv2._read_messages())
    finally:
        sys.stdin = old_stdin
    check("两种分帧都能解析", len(msgs) == 2, f"{len(msgs)} 条")
    check("分帧 JSON 可反序列化",
          all(json.loads(m)["method"] == "ping" for m in msgs))

    failed = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        extra = f"  ({detail})" if detail and not ok else ""
        print(f"[{mark}] {name}{extra}")
    print(f"\n{len(checks) - len(failed)}/{len(checks)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RemoteConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        sys.exit(3)
