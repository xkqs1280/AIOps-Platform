"""设备 CLI 交互终端 WebSocket 端点

协议（JSON over WebSocket）：
- 客户端 → 服务端：
    {"type": "input", "data": "<原始终端输入>"}
    {"type": "resize", "cols": 120, "rows": 32}
    {"type": "ping"}
- 服务端 → 客户端：
    {"type": "output", "data": "<设备输出>"}
    {"type": "error", "message": "..."}
    {"type": "closed", "message": "..."}
    {"type": "pong"}

认证：与 HTTP 一致，cookie `access_token` 或 `Authorization: Bearer`。
"""
import asyncio
import json
import logging
import os
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.device import Device
from app.routers.auth import current_user, operator_or_admin
from app.services.audit_service import record_audit
from app.services.credential_service import reveal_secret
from app.services.terminal_log_service import get_terminal_log_store
from app.services.terminal_service import DeviceTerminal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/devices", tags=["设备终端"])

# 每个用户同时最多打开的终端会话数（防单个账号抢占设备 vty / 资源滥用）
MAX_TERMINAL_SESSIONS_PER_USER = int(os.environ.get("AIOPS_TERMINAL_MAX_SESSIONS", "5"))
# 用户 -> 当前活动终端会话数（事件循环单线程内增减，无并发竞争）
_active_sessions: dict[str, int] = {}


@router.websocket("/{device_id}/terminal")
async def device_terminal_ws(
    websocket: WebSocket,
    device_id: int,
    db: AsyncSession = Depends(get_db),
):
    """设备 CLI 交互终端（WebSocket 双向流）。"""
    # 必须先 accept 才能发送/关闭（Starlette 约束：未 accept 的 close 会抛错导致 403）
    await websocket.accept()

    # 访问 IP 白名单拦截：同上，HTTP 中间件不作用于 WebSocket 升级请求，
    # 不在这里补一道的话，白名单外的 IP 仍能通过本端点拿到设备 CLI。
    from app.services.ip_whitelist import is_allowed, resolve_client_ip, whitelist_state
    _client_ip = resolve_client_ip(websocket)
    _wl_enabled, _wl_networks = await whitelist_state()
    if _wl_enabled and not is_allowed(_client_ip, _wl_networks):
        logger.warning("IP 白名单拦截 WebSocket 终端：ip=%s device_id=%s", _client_ip, device_id)
        await websocket.close(code=4003, reason="当前来源 IP 不在平台访问白名单内")
        return

    # License 锁定拦截：授权中间件（main.py HTTP 中间件）不作用于 WebSocket，
    # 若不在 accept 后立即校验，锁定态下持账号者仍可经本端点无痕操作全网设备。
    from app.services.license_service import is_locked
    if await is_locked():
        await websocket.close(code=4003, reason="平台授权已锁定，设备终端不可用")
        return

    # 认证：WebSocket 握手无法用 HTTP Depends，手动校验 token（cookie / Bearer / query 参数）
    token = websocket.cookies.get("access_token")
    authorization = websocket.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token:
        token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="未认证")
        return
    try:
        actor = await current_user(
            access_token=token,
            authorization=websocket.headers.get("authorization"),
            db=db,
        )
    except Exception:
        await websocket.close(code=4001, reason="未认证")
        return
    if not actor:
        await websocket.close(code=4001, reason="未认证")
        return

    # 角色校验：viewer（只读角色）无权对设备执行 CLI 命令。
    # WebSocket 升级请求不走 HTTP 中间件的写方法拦截，必须在此显式拦截。
    if actor.get("role") == "viewer":
        await websocket.close(code=4003, reason="只读角色无权使用设备终端")
        return

    # per-user 终端会话数上限（防单个账号开启大量会话压垮设备 vty）
    username_key = actor.get("sub") or "?"
    cur = _active_sessions.get(username_key, 0)
    if cur >= MAX_TERMINAL_SESSIONS_PER_USER:
        await websocket.close(code=4003, reason=f"终端会话数已达上限（{MAX_TERMINAL_SESSIONS_PER_USER}），请先关闭其他终端")
        return
    _active_sessions[username_key] = cur + 1

    def _release_session():
        remaining = _active_sessions.get(username_key, 0) - 1
        if remaining <= 0:
            _active_sessions.pop(username_key, None)
        else:
            _active_sessions[username_key] = remaining

    # 加载设备
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        _release_session()
        await websocket.close(code=4004, reason="设备不存在")
        return

    # 凭据
    username = reveal_secret(device.mgmt_username) or ""
    password = reveal_secret(device.mgmt_password) or ""
    if not username or not password:
        _release_session()
        await websocket.close(code=4003, reason="设备未配置管理账号或密码")
        return

    proto = device.mgmt_protocol or "ssh"
    # 使用设备配置的管理端口（mgmt_port），未配置时按协议默认 ssh=22 / telnet=23
    dev_port = device.mgmt_port or (23 if proto == "telnet" else 22)

    terminal = DeviceTerminal(
        ip=device.ip,
        username=username,
        password=password,
        protocol=proto,
        port=dev_port,
        cols=120,
        rows=32,
    )

    # ---- 会话日志：每次连接默认开启（华哥 2026-10-21 需求）----
    # 文件命名 <sysname>-<设备IP>-<YYYYMMDD-HHMM>.log，实时落盘，供「本次会话日志下载」。
    # 创建失败绝不能影响终端本身 —— 日志是辅助能力，不是终端的前置条件。
    session_log = None
    if settings.TERMINAL_LOG_ENABLED:
        try:
            session_log = get_terminal_log_store().create(
                device_name=device.name,
                ip=device.ip,
                operator=username_key,
                target=f"{proto}:{dev_port}",
            )
        except Exception as e:
            logger.warning("终端会话日志创建失败 %s: %s", device.ip, e)
            session_log = None

    output_queue: asyncio.Queue = asyncio.Queue()

    async def push_output(data: str) -> None:
        # 先落盘再推送：即使 WebSocket 随即断开，设备已吐出的内容也不会丢
        if session_log is not None:
            session_log.write(data)
        await output_queue.put(json.dumps({"type": "output", "data": data}, ensure_ascii=False))

    async def on_closed(reason: str) -> None:
        await output_queue.put(json.dumps({"type": "closed", "message": reason}, ensure_ascii=False))

    terminal.on_output = push_output
    terminal.on_closed = on_closed

    # 建立连接
    try:
        await terminal.connect(timeout=25)
    except asyncio.TimeoutError:
        _release_session()
        if session_log is not None:
            session_log.unlink_if_trivial()
        await websocket.send_text(json.dumps({"type": "error", "message": "连接设备超时"}, ensure_ascii=False))
        await websocket.close()
        return
    except Exception as e:
        _release_session()
        if session_log is not None:
            session_log.unlink_if_trivial()
        await websocket.send_text(json.dumps({"type": "error", "message": f"连接失败: {e}"}, ensure_ascii=False))
        await websocket.close()
        return

    # 告知前端本次会话的日志文件名（「本次会话日志下载」按钮据此启用）。
    # ⚠️ 必须在 pump() 启动前发 —— 一旦 pump 跑起来，它和这里会同时往同一个
    # WebSocket 写，Starlette 下并发 send 会抛异常。
    if session_log is not None:
        try:
            await websocket.send_text(json.dumps({
                "type": "log",
                "enabled": True,
                "filename": session_log.filename,
            }, ensure_ascii=False))
        except Exception:
            pass

    # 审计：终端会话开始
    try:
        await record_audit(
            db, actor, "device", "terminal",
            f"CLI 终端会话开始：{device.name}({device.ip})",
        )
        await db.commit()
    except Exception:
        pass

    # 后台输出推送
    async def pump():
        try:
            while True:
                msg = await output_queue.get()
                await websocket.send_text(msg)
        except Exception:
            pass

    read_task = asyncio.create_task(terminal.start_output_loop())
    pump_task = asyncio.create_task(pump())

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")
            if mtype == "input":
                data = msg.get("data", "")
                if data:
                    await terminal.send(data)
            elif mtype == "resize":
                await terminal.resize(int(msg.get("cols", 120)), int(msg.get("rows", 32)))
            elif mtype == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}, ensure_ascii=False))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"terminal ws error {device.ip}: {e}")
    finally:
        await terminal.close()
        # 收尾会话日志：补最后一行 + 写页脚（含时长/大小/结束原因）
        if session_log is not None:
            session_log.close("会话结束")
        read_task.cancel()
        pump_task.cancel()
        try:
            await asyncio.gather(read_task, pump_task, return_exceptions=True)
        except Exception:
            pass
        # 释放 per-user 会话计数
        _release_session()
        # 审计：会话结束
        try:
            await record_audit(
                db, actor, "device", "terminal_close",
                f"CLI 终端会话结束：{device.name}({device.ip})",
            )
            await db.commit()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 会话日志查询 / 下载（HTTP）
# ---------------------------------------------------------------------------
# 权限口径与 WebSocket 终端一致：viewer 不能开终端，自然也看不到 CLI 输出。


@router.get("/{device_id}/terminal/logs")
async def list_terminal_logs(
    device_id: int,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(operator_or_admin),
):
    """列出某台设备的历史 CLI 会话日志（新 → 旧）。"""
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    if not settings.TERMINAL_LOG_ENABLED:
        return {"enabled": False, "items": []}

    safe_limit = max(1, min(int(limit or 20), 200))
    items = get_terminal_log_store().list_for(device.name, device.ip, limit=safe_limit)
    return {"enabled": True, "items": items}


@router.get("/{device_id}/terminal/logs/{filename}")
async def download_terminal_log(
    device_id: int,
    filename: str,
    db: AsyncSession = Depends(get_db),
    _: dict = Depends(operator_or_admin),
):
    """下载某次会话的日志文件。

    文件名在服务端做三重校验（纯文件名 / 必须属于本设备 / 必须落在日志目录内），
    不信任前端传来的任何路径片段。
    """
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    if not settings.TERMINAL_LOG_ENABLED:
        raise HTTPException(status_code=404, detail="会话日志功能已关闭")

    path = get_terminal_log_store().resolve(device.name, device.ip, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="日志文件不存在或不属于该设备")

    return FileResponse(
        path,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(path.name)}",
            # 日志在会话进行中仍会被追加，禁止缓存半截内容
            "Cache-Control": "no-store",
        },
    )
