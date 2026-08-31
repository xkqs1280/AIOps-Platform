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

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.device import Device
from app.routers.auth import current_user
from app.services.audit_service import record_audit
from app.services.credential_service import reveal_secret
from app.services.terminal_service import DeviceTerminal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/devices", tags=["设备终端"])


@router.websocket("/{device_id}/terminal")
async def device_terminal_ws(
    websocket: WebSocket,
    device_id: int,
    db: AsyncSession = Depends(get_db),
):
    """设备 CLI 交互终端（WebSocket 双向流）。"""
    # 必须先 accept 才能发送/关闭（Starlette 约束：未 accept 的 close 会抛错导致 403）
    await websocket.accept()

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

    # 加载设备
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        await websocket.close(code=4004, reason="设备不存在")
        return

    # 凭据
    username = reveal_secret(device.mgmt_username) or ""
    password = reveal_secret(device.mgmt_password) or ""
    if not username or not password:
        await websocket.close(code=4003, reason="设备未配置管理账号或密码")
        return

    terminal = DeviceTerminal(
        ip=device.ip,
        username=username,
        password=password,
        protocol=device.mgmt_protocol or "ssh",
        # 使用设备配置的管理端口（mgmt_port），未配置时按协议默认 ssh=22 / telnet=23
        port=device.mgmt_port or (23 if (device.mgmt_protocol or "ssh") == "telnet" else 22),
        cols=120,
        rows=32,
    )

    output_queue: asyncio.Queue = asyncio.Queue()

    async def push_output(data: str) -> None:
        await output_queue.put(json.dumps({"type": "output", "data": data}, ensure_ascii=False))

    async def on_closed(reason: str) -> None:
        await output_queue.put(json.dumps({"type": "closed", "message": reason}, ensure_ascii=False))

    terminal.on_output = push_output
    terminal.on_closed = on_closed

    # 建立连接
    try:
        await terminal.connect(timeout=25)
    except asyncio.TimeoutError:
        await websocket.send_text(json.dumps({"type": "error", "message": "连接设备超时"}, ensure_ascii=False))
        await websocket.close()
        return
    except Exception as e:
        await websocket.send_text(json.dumps({"type": "error", "message": f"连接失败: {e}"}, ensure_ascii=False))
        await websocket.close()
        return

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
        read_task.cancel()
        pump_task.cancel()
        try:
            await asyncio.gather(read_task, pump_task, return_exceptions=True)
        except Exception:
            pass
        # 审计：会话结束
        try:
            await record_audit(
                db, actor, "device", "terminal_close",
                f"CLI 终端会话结束：{device.name}({device.ip})",
            )
            await db.commit()
        except Exception:
            pass
