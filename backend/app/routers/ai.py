# -*- coding: utf-8 -*-
"""AI 辅助 API 路由：助手对话（SSE）、告警解读、配置差异分析、巡检总结、
CLI 命令建议、运维日报、知识库（RAG）、调用审计。"""
import difflib
import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session, get_db
from app.models.alert import Alert
from app.models.config_backup import ConfigBackup
from app.models.device import Device
from app.models.inspection import InspectionDeviceResult, InspectionTask
from app.routers.auth import admin_only, current_user
from app.services import ai_service as svc

router = APIRouter(prefix="/ai", tags=["AI 辅助"])

KB_MAX_SIZE = 2 * 1024 * 1024  # 2MB
KB_SUFFIXES = (".txt", ".md", ".log", ".csv", ".conf", ".cfg")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

async def _sse_response(scene: str, user: str, target: str, agen, *, db: AsyncSession,
                        cache_key: str | None = None, model: str = ""):
    """包装为 SSE 流；结束后写审计与可选缓存。"""
    t0 = time.monotonic()
    parts: list[str] = []

    async def wrapper():
        nonlocal parts
        ok, err = True, None
        try:
            async for delta in agen:
                parts.append(delta)
                yield f"data: {json.dumps({'t': delta}, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            ok, err = False, str(e)[:300]
            yield f"data: {json.dumps({'error': err}, ensure_ascii=False)}\n\n"
        duration = int((time.monotonic() - t0) * 1000)
        # 缓存与审计必须在 [DONE] 之前完成：客户端收到 [DONE] 即断开，
        # 此后再 await 会被 uvicorn 取消导致写入丢失。
        try:
            if cache_key and ok and parts:
                await svc.cache_put(db, cache_key, scene, "".join(parts), model)
        except Exception:  # 缓存失败不影响输出
            pass
        try:
            async with async_session() as s2:
                await svc.log_ai(db=s2, user=user, scene=scene, target=target,
                                 model=model, ok=ok, duration_ms=duration, error=err)
        except Exception:
            pass
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        wrapper(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


async def _cached_sse(scene: str, cache_key: str, user: str, target: str,
                      db: AsyncSession, messages_fn):
    """带缓存的场景解读：命中直接推送，未命中流式生成后写缓存。"""
    cached = await svc.cache_get(db, cache_key)
    if cached is not None:
        # 命中路径直接输出帧，绝不经过 _sse_response 二次包装
        #（wrapper 会把 delta 再包一层 data: 帧，导致客户端收到原始帧文本）。
        t0 = time.monotonic()

        async def one_shot():
            yield f"data: {json.dumps({'t': cached, 'cached': True}, ensure_ascii=False)}\n\n"
            duration = int((time.monotonic() - t0) * 1000)
            try:
                async with async_session() as s2:
                    await svc.log_ai(db=s2, user=user, scene=scene, target=target,
                                     model="", ok=True, duration_ms=duration, error=None)
            except Exception:
                pass
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            one_shot(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    async def _gen():
        async with svc._ai_semaphore:
            async for delta in svc.stream_chat(db, messages_fn()):
                yield delta
    return await _sse_response(scene, user, target, _gen(), db=db, cache_key=cache_key)


async def _require_device(db: AsyncSession, device_id: int) -> Device:
    d = (await db.execute(select(Device).where(Device.id == device_id))).scalars().first()
    if not d:
        raise HTTPException(404, "设备不存在")
    return d


# ---------------------------------------------------------------------------
# 配置与连接测试
# ---------------------------------------------------------------------------

class AiConfigBody(BaseModel):
    enabled: bool = False
    provider: str = Field("ollama", max_length=16)
    base_url: str = Field("", max_length=255)
    model: str = Field("", max_length=64)
    api_key: str = Field("", max_length=255)  # 掩码原样=不修改
    temperature: float = Field(0.3, ge=0, le=2)
    embed_model: str = Field("", max_length=64)


@router.get("/config")
async def read_config(db: AsyncSession = Depends(get_db), _: dict = Depends(admin_only)):
    return await svc.get_ai_setting(db)


@router.post("/config")
async def update_config(body: AiConfigBody, db: AsyncSession = Depends(get_db), actor: dict = Depends(admin_only)):
    return await svc.save_ai_setting(db, body.model_dump())


@router.post("/test")
async def test_connection(db: AsyncSession = Depends(get_db), _: dict = Depends(admin_only)):
    return await svc.test_connection(db)


# ---------------------------------------------------------------------------
# 全局助手对话（SSE）
# ---------------------------------------------------------------------------

class ChatBody(BaseModel):
    messages: list[dict] = Field(..., description="[{role, content}]，含历史对话")


@router.post("/chat")
async def chat(body: ChatBody, db: AsyncSession = Depends(get_db), actor: dict = Depends(current_user)):
    msgs = [m for m in body.messages[-20:] if m.get("role") in ("system", "user", "assistant")]
    if not msgs or msgs[-1].get("role") != "user":
        raise HTTPException(400, "最后一条消息必须来自用户")
    # 注入系统提示 + 知识库检索上下文
    if not any(m.get("role") == "system" for m in msgs):
        system = svc.SYSTEM_BASE
        try:
            kb_ctx = await svc.kb_build_context(db, msgs[-1]["content"])
        except Exception:
            kb_ctx = ""
        if kb_ctx:
            system += "\n\n## 知识库参考片段\n" + kb_ctx
        msgs = [{"role": "system", "content": system}] + msgs

    async def _gen():
        async with svc._ai_semaphore:
            async for delta in svc.stream_chat(db, msgs):
                yield delta
    return await _sse_response("chat", actor.get("username", ""), msgs[-1]["content"][:120], _gen(), db=db)


# ---------------------------------------------------------------------------
# 告警 AI 解读
# ---------------------------------------------------------------------------

@router.post("/explain/alert/{alert_id}")
async def explain_alert(alert_id: int, db: AsyncSession = Depends(get_db), actor: dict = Depends(current_user)):
    a = (await db.execute(select(Alert).where(Alert.id == alert_id))).scalars().first()
    if not a:
        raise HTTPException(404, "告警不存在")
    d = await _require_device(db, a.device_id)
    recent = await svc.collect_recent_alerts_text(db, a.device_id, a.id)
    ctx = {
        "rule_name": a.rule_name, "severity": a.severity, "message": a.message,
        "triggered_at": str(a.triggered_at), "status": a.status,
        "device": svc._device_brief(d), "recent": recent,
    }
    key = f"alert:{alert_id}:{a.message[:60]}"
    return await _cached_sse("alert", key, actor.get("username", ""), f"alert#{alert_id}",
                             db, lambda: svc.build_alert_messages(ctx))


# ---------------------------------------------------------------------------
# 配置备份差异解读
# ---------------------------------------------------------------------------

@router.post("/explain/backup/{backup_id}")
async def explain_backup(backup_id: int, db: AsyncSession = Depends(get_db), actor: dict = Depends(current_user)):
    cur = (await db.execute(select(ConfigBackup).where(ConfigBackup.id == backup_id))).scalars().first()
    if not cur:
        raise HTTPException(404, "备份记录不存在")
    d = await _require_device(db, cur.device_id)
    prev = (await db.execute(
        select(ConfigBackup).where(
            ConfigBackup.device_id == cur.device_id, ConfigBackup.id < backup_id, ConfigBackup.status == "success"
        ).order_by(ConfigBackup.id.desc()).limit(1)
    )).scalars().first()
    diff = "(无更早的成功备份，无法比较)"
    if prev:
        lines = list(difflib.unified_diff(
            (prev.config_content or "").splitlines(), (cur.config_content or "").splitlines(),
            fromfile=f"旧配置#{prev.id}", tofile=f"新配置#{cur.id}", lineterm="", n=2,
        ))
        diff = "\n".join(lines[:400]) or "（两次配置完全一致）"
    ctx = {"device": svc._device_brief(d), "diff": svc.sanitize(diff)}
    key = f"backup:{backup_id}:{cur.config_hash or ''}"
    return await _cached_sse("backup", key, actor.get("username", ""), f"backup#{backup_id}",
                             db, lambda: svc.build_backup_messages(ctx))


# ---------------------------------------------------------------------------
# 巡检报告 AI 总结
# ---------------------------------------------------------------------------

def _brief_parsed(parsed: dict | None) -> str:
    if not isinstance(parsed, dict):
        return ""
    keep = {}
    for k, v in parsed.items():
        s = str(v)
        keep[str(k)] = s[:120]
    return json.dumps(keep, ensure_ascii=False)[:400]


@router.post("/summary/inspection/{task_id}")
async def summarize_inspection(task_id: int, db: AsyncSession = Depends(get_db), actor: dict = Depends(current_user)):
    task = (await db.execute(select(InspectionTask).where(InspectionTask.id == task_id))).scalars().first()
    if not task:
        raise HTTPException(404, "巡检任务不存在")
    results = (await db.execute(
        select(InspectionDeviceResult).where(InspectionDeviceResult.task_id == task_id)
        .order_by(InspectionDeviceResult.id).limit(60)
    )).scalars().all()
    lines = []
    for r in results:
        if r.status == "success":
            lines.append(f"- {r.device_name}({r.device_ip}): 成功；摘要 {_brief_parsed(r.parsed_data)}")
        else:
            lines.append(f"- {r.device_name}({r.device_ip}): {r.status}；{r.error_message or ''}"[:300])
    ctx = {
        "name": task.name, "total": task.total_devices,
        "success": task.success_count, "failed": task.failed_count,
        "results": "\n".join(lines) or "（无结果明细）",
    }
    key = f"inspection:{task_id}:{task.completed_at or ''}"
    return await _cached_sse("inspection", key, actor.get("username", ""), f"task#{task_id}",
                             db, lambda: svc.build_inspection_messages(ctx))


# ---------------------------------------------------------------------------
# CLI 命令助手
# ---------------------------------------------------------------------------

class CliAdviceBody(BaseModel):
    device_id: int
    question: str = Field(..., max_length=500)


@router.post("/cli/advice")
async def cli_advice(body: CliAdviceBody, db: AsyncSession = Depends(get_db), actor: dict = Depends(current_user)):
    d = await _require_device(db, body.device_id)
    ctx = {"device": svc._device_brief(d), "question": svc.sanitize(body.question)}
    async def _gen():
        async with svc._ai_semaphore:
            async for delta in svc.stream_chat(db, svc.build_cli_messages(ctx)):
                yield delta
    return await _sse_response("cli", actor.get("username", ""), f"device#{body.device_id}", _gen(), db=db)


# ---------------------------------------------------------------------------
# 运维日报（当日缓存）
# ---------------------------------------------------------------------------

@router.post("/report/daily")
async def daily_report(db: AsyncSession = Depends(get_db), actor: dict = Depends(current_user)):
    ctx = await svc.collect_alert_stats_text(db)
    key = f"report:{datetime.now().strftime('%Y%m%d')}"
    return await _cached_sse("report", key, actor.get("username", ""), "daily",
                             db, lambda: svc.build_report_messages(ctx))


# ---------------------------------------------------------------------------
# 知识库（RAG）
# ---------------------------------------------------------------------------

@router.post("/kb/upload")
async def kb_upload(file: UploadFile = File(...), note: str = "",
                    db: AsyncSession = Depends(get_db), _: dict = Depends(admin_only)):
    raw = await file.read()
    if len(raw) > KB_MAX_SIZE:
        raise HTTPException(400, "文件超过 2MB 限制")
    name = file.filename or "untitled.txt"
    if not name.lower().endswith(KB_SUFFIXES):
        raise HTTPException(400, "仅支持 txt/md/log/csv/conf/cfg 文本文件")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            content = raw.decode("gb18030")  # 兼容中文 GBK 文档
        except UnicodeDecodeError:
            raise HTTPException(400, "文件编码无法识别（需 UTF-8 或 GBK）")
    result = await svc.kb_add_document(db, name, content, note)
    return result


@router.get("/kb/docs")
async def kb_docs(db: AsyncSession = Depends(get_db), _: dict = Depends(admin_only)):
    from app.models.ai import AiKbDoc
    rows = (await db.execute(select(AiKbDoc).order_by(AiKbDoc.id.desc()))).scalars().all()
    return {"items": [
        {"id": d.id, "filename": d.filename, "size": d.size, "chunk_count": d.chunk_count,
         "status": d.status, "note": d.note,
         "created_at": d.created_at.isoformat() if d.created_at else None}
        for d in rows
    ]}


@router.delete("/kb/docs/{doc_id}")
async def kb_delete(doc_id: int, db: AsyncSession = Depends(get_db), _: dict = Depends(admin_only)):
    from app.models.ai import AiKbChunk, AiKbDoc
    await db.execute(AiKbChunk.__table__.delete().where(AiKbChunk.doc_id == doc_id))
    await db.execute(AiKbDoc.__table__.delete().where(AiKbDoc.id == doc_id))
    await db.commit()
    return {"ok": True}


class KbSearchBody(BaseModel):
    query: str = Field(..., max_length=500)


@router.post("/kb/search")
async def kb_search(body: KbSearchBody, db: AsyncSession = Depends(get_db), _: dict = Depends(admin_only)):
    return {"items": await svc.kb_search(db, body.query)}


# ---------------------------------------------------------------------------
# 调用审计日志
# ---------------------------------------------------------------------------

@router.get("/logs")
async def ai_logs(page: int = 1, page_size: int = 20,
                  db: AsyncSession = Depends(get_db), _: dict = Depends(admin_only)):
    from sqlalchemy import func
    from app.models.ai import AiLog
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    total = (await db.execute(select(func.count(AiLog.id)))).scalar() or 0
    rows = (await db.execute(
        select(AiLog).order_by(AiLog.id.desc()).offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()
    return {"total": total, "items": [
        {"id": r.id, "user": r.user, "scene": r.scene, "target": r.target, "model": r.model,
         "ok": r.ok, "duration_ms": r.duration_ms, "error": r.error,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows
    ]}
