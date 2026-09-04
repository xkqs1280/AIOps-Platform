# -*- coding: utf-8 -*-
"""AI 辅助服务：OpenAI 兼容协议接入（Ollama / 内网网关 / 云端 API 可切换）。

设计要点：
  - 统一走 {base_url}/chat/completions（OpenAI 兼容），Ollama 原生支持 /v1 前缀；
  - 敏感脱敏：SNMP community / 密码 / 凭据等正则替换，设备密码字段永不进入上下文；
  - 结果缓存：同场景同目标 24h 内复用（ai_cache 表），避免重复推理；
  - 知识库（RAG）：文档切块 → embedding（/v1/embeddings）→ 余弦 + 关键词混合检索，
    embedding 失败自动降级纯关键词检索，零外部依赖（不引 ChromaDB）；
  - 调用审计：每次 AI 调用写 ai_logs。
"""
import asyncio
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import AiCache, AiKbChunk, AiKbDoc, AiLog, AiSetting
from app.services.credential_service import protect_secret, reveal_secret

logger = logging.getLogger("aiops.ai")

# .env 可覆盖的默认接入参数（未在设置页配置时兜底）
DEFAULT_BASE_URL = os.getenv("AI_BASE_URL", "http://127.0.0.1:11434/v1")
DEFAULT_MODEL = os.getenv("AI_MODEL", "qwen2.5:7b-instruct")
DEFAULT_EMBED_MODEL = os.getenv("AI_EMBED_MODEL", "nomic-embed-text")
CACHE_TTL_HOURS = 24
TIMEOUT_SECONDS = 180

# 脱敏：community/password/secret/credential 赋值行打码
_SANITIZE_RE = re.compile(
    r"(?i)\b(snmp[-_ ]?community|community|password|passwd|secret|credential|private[-_ ]?key)\b(\s*[:=]\s*)(\S+)"
)


def sanitize(text: str) -> str:
    """脱敏文本中疑似凭据的赋值内容。"""
    return _SANITIZE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}******", text or "")


# 告警级别中英文映射（供日报/解读输出中文）
SEVERITY_CN = {
    "critical": "严重",
    "major": "重要",
    "minor": "次要",
    "warning": "警告",
    "info": "提示",
}


def sev_cn(s: str | None) -> str:
    return SEVERITY_CN.get((s or "").lower(), s or "未知")


# ---------------------------------------------------------------------------
# 接入配置
# ---------------------------------------------------------------------------

def _mask_key(key: str) -> str:
    if not key:
        return ""
    return key[:3] + "****" + key[-3:] if len(key) > 8 else "****"


async def get_ai_setting(db: AsyncSession) -> dict:
    row = (await db.execute(select(AiSetting))).scalars().first()
    if not row:
        return {
            "enabled": False, "provider": "ollama", "base_url": DEFAULT_BASE_URL,
            "model": DEFAULT_MODEL, "api_key": "", "temperature": 0.3,
            "embed_model": DEFAULT_EMBED_MODEL, "configured": False,
        }
    return {
        "enabled": row.enabled,
        "provider": row.provider,
        "base_url": row.base_url,
        "model": row.model,
        "api_key": _mask_key(reveal_secret(row.api_key) or ""),
        "temperature": (row.temperature or 30) / 100.0,
        "embed_model": row.embed_model,
        "configured": True,
    }


async def save_ai_setting(db: AsyncSession, data: dict) -> dict:
    row = (await db.execute(select(AiSetting))).scalars().first()
    if not row:
        row = AiSetting()
        db.add(row)
    row.enabled = bool(data.get("enabled", False))
    row.provider = (data.get("provider") or "ollama")[:16]
    row.base_url = (data.get("base_url") or DEFAULT_BASE_URL).strip()[:255]
    row.model = (data.get("model") or DEFAULT_MODEL).strip()[:64]
    new_key = (data.get("api_key") or "").strip()
    if new_key and "****" not in new_key:  # 掩码原样返回表示不修改
        row.api_key = protect_secret(new_key) or ""
    row.temperature = int(round(float(data.get("temperature", 0.3)) * 100))
    row.embed_model = (data.get("embed_model") or DEFAULT_EMBED_MODEL).strip()[:64]
    await db.commit()
    return await get_ai_setting(db)


async def _effective_cfg(db: AsyncSession) -> dict:
    """读取配置；未配置时使用 .env 默认值（enabled 视为配置页开关）。"""
    row = (await db.execute(select(AiSetting))).scalars().first()
    if row:
        return {
            "enabled": row.enabled, "base_url": row.base_url or DEFAULT_BASE_URL,
            "model": row.model or DEFAULT_MODEL,
            "api_key": reveal_secret(row.api_key) or "",
            "temperature": (row.temperature or 30) / 100.0,
            "embed_model": row.embed_model or DEFAULT_EMBED_MODEL,
        }
    return {
        "enabled": False, "base_url": DEFAULT_BASE_URL, "model": DEFAULT_MODEL,
        "api_key": "", "temperature": 0.3, "embed_model": DEFAULT_EMBED_MODEL,
    }


# ---------------------------------------------------------------------------
# OpenAI 兼容调用（流式 / 聚合）
# ---------------------------------------------------------------------------

def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


async def stream_chat(db: AsyncSession, messages: list[dict], *, temperature: float | None = None):
    """流式对话：逐段 yield 文本增量。失败抛异常（由路由包装成错误帧）。"""
    cfg = await _effective_cfg(db)
    if not cfg["enabled"]:
        raise RuntimeError("AI 功能未启用，请先在系统设置中配置 AI 接入")
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "stream": True,
        "temperature": cfg["temperature"] if temperature is None else temperature,
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        async with client.stream("POST", url, json=payload, headers=_headers(cfg["api_key"])) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode(errors="replace")[:300]
                raise RuntimeError(f"模型服务返回 {resp.status_code}: {body}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                choices = obj.get("choices") or [{}]
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    yield delta


async def collect_chat(db: AsyncSession, messages: list[dict], *, temperature: float | None = None) -> str:
    """聚合版：拼接流式输出为完整文本。"""
    parts = []
    async for chunk in stream_chat(db, messages, temperature=temperature):
        parts.append(chunk)
    return "".join(parts)


async def test_connection(db: AsyncSession, override: dict | None = None) -> dict:
    """连接测试：直接调用 chat/completions（非流式，max_tokens=8 探活）。"""
    cfg = await _effective_cfg(db)
    if override:
        cfg.update({k: v for k, v in override.items() if v})
        if override.get("enabled") is not None:
            cfg["enabled"] = True  # 测试时忽略启用开关
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": "回复 OK"}],
        "max_tokens": 8, "temperature": 0,
    }
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=_headers(cfg["api_key"]))
            latency = int((time.monotonic() - t0) * 1000)
            if resp.status_code != 200:
                return {"ok": False, "latency_ms": latency, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
            content = ((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content", "")
            return {"ok": True, "latency_ms": latency, "reply": content[:50]}
    except Exception as e:
        return {"ok": False, "latency_ms": int((time.monotonic() - t0) * 1000), "error": str(e)[:200]}


# ---------------------------------------------------------------------------
# 缓存与审计
# ---------------------------------------------------------------------------

async def cache_get(db: AsyncSession, cache_key: str) -> str | None:
    row = (await db.execute(select(AiCache).where(AiCache.cache_key == cache_key))).scalars().first()
    if not row:
        return None
    created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - created > timedelta(hours=CACHE_TTL_HOURS):
        await db.execute(delete(AiCache).where(AiCache.id == row.id))
        await db.commit()
        return None
    return row.content


async def cache_put(db: AsyncSession, cache_key: str, scene: str, content: str, model: str = "") -> None:
    await db.execute(delete(AiCache).where(AiCache.cache_key == cache_key))
    db.add(AiCache(cache_key=cache_key[:160], scene=scene[:32], content=content, model=model[:64]))
    await db.commit()


async def log_ai(db: AsyncSession, *, user: str, scene: str, target: str = "",
                 provider: str = "", model: str = "", ok: bool = True,
                 duration_ms: int = 0, error: str | None = None) -> None:
    try:
        db.add(AiLog(user=user[:64], scene=scene[:32], target=target[:255], provider=provider[:16],
                     model=model[:64], ok=ok, duration_ms=duration_ms, error=(error or "")[:255]))
        await db.commit()
    except Exception as e:  # 审计失败不影响主流程
        logger.warning("ai log failed: %s", e)


# ---------------------------------------------------------------------------
# 系统提示词与场景 Prompt
# ---------------------------------------------------------------------------

SYSTEM_BASE = (
    "你是 AIOps 智能运维托管平台的 AI 运维专家，精通华为、H3C（Comware）等网络与安全设备的运维排障。"
    "规则：1) 仅基于给定的上下文作答，上下文不足时明确说明，不要编造；"
    "2) 给出的配置/命令必须标注「需人工确认后执行」，禁止声称会自动下发；"
    "3) 用简体中文、Markdown 输出，结论先行、简明扼要；"
    "4) 不输出任何密码、团体字等敏感凭据。"
)

CLI_HINT = (
    "输出格式：先一句话结论，再给「处置建议」小节（含命令片段，用代码块标注厂商语法），"
    "最后给「风险提示」。"
)


def build_alert_messages(alert_ctx: dict) -> list[dict]:
    user = (
        f"请解读以下告警并给出处置建议。\n\n"
        f"## 告警信息\n- 规则: {alert_ctx['rule_name']}\n- 级别: {alert_ctx['severity']}\n"
        f"- 内容: {alert_ctx['message']}\n- 触发时间: {alert_ctx['triggered_at']}\n- 状态: {alert_ctx['status']}\n\n"
        f"## 设备信息\n{alert_ctx['device']}\n\n"
        f"## 同设备近期告警（最多5条）\n{alert_ctx['recent']}\n\n{CLI_HINT}"
    )
    return [{"role": "system", "content": SYSTEM_BASE}, {"role": "user", "content": user}]


def build_backup_messages(ctx: dict) -> list[dict]:
    user = (
        f"请分析该设备配置变更的差异，评估风险并给出建议。\n\n"
        f"## 设备信息\n{ctx['device']}\n\n"
        f"## 配置差异（unified diff，旧→新）\n```diff\n{ctx['diff']}\n```\n\n"
        f"差异为空则说明两次配置一致，请直接说明。输出：变更要点 → 风险评估（高/中/低）→ 建议。"
    )
    return [{"role": "system", "content": SYSTEM_BASE}, {"role": "user", "content": user}]


def build_inspection_messages(ctx: dict) -> list[dict]:
    user = (
        f"请总结以下巡检结果并生成中文结论。\n\n"
        f"## 巡检任务\n名称: {ctx['name']}，设备总数 {ctx['total']}，成功 {ctx['success']}，失败 {ctx['failed']}\n\n"
        f"## 各设备结果摘要\n{ctx['results']}\n\n"
        f"输出：总体评价 → 异常设备清单与原因 → 整改建议（按优先级）。"
    )
    return [{"role": "system", "content": SYSTEM_BASE}, {"role": "user", "content": user}]


def build_cli_messages(ctx: dict) -> list[dict]:
    user = (
        f"用户想完成以下操作，请给出对应命令与步骤。\n\n"
        f"## 设备信息\n{ctx['device']}\n\n"
        f"## 需求\n{ctx['question']}\n\n"
        f"按该厂商语法给出命令片段（代码块），逐条解释作用，并提示影响范围。{CLI_HINT}"
    )
    return [{"role": "system", "content": SYSTEM_BASE}, {"role": "user", "content": user}]


def build_report_messages(ctx: dict) -> list[dict]:
    user = (
        f"请基于以下平台运行数据，生成一份可直接呈报领导的《网络运维日报》（Markdown）。\n\n{ctx}\n\n"
        f"写作要求：\n"
        f"1. 口吻正式专业、结论先行，面向不熟悉技术细节的管理者；避免出现「平台未提供」「暂无法」这类暴露数据缺口的表述，"
        f"数据不足时改为给出现状判断与下一步核实动作。\n"
        f"2. 告警级别一律用中文表述（严重/重要/次要/警告/提示），设备状态用「在线/离线」。\n"
        f"3. 输出结构：\n"
        f"   # 网络运维日报（日期）\n"
        f"   **一句话摘要**：用 2~3 句概括整体健康度、关键风险与今日工作重点。\n"
        f"   ## 一、运行总览（表格：设备总数/在线/离线、告警总数、最严重级别）\n"
        f"   ## 二、重点告警与影响分析（逐条：涉及设备 → 告警内容 → 可能影响 → 当前状态）\n"
        f"   ## 三、风险研判（按高/中/低分级，说明理由）\n"
        f"   ## 四、已采取措施与工作建议（区分「已完成/待跟进」，每条注明责任方向）\n"
        f"4. 数据充分时给出明确判断；只有一条严重告警时也必须结合告警明细内容分析可能原因，不要泛泛而谈。\n"
        f"5. 全文控制在一页以内，关键结论加粗。"
    )
    return [{"role": "system", "content": SYSTEM_BASE}, {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# 知识库（RAG）：切块 / 向量化 / 混合检索
# ---------------------------------------------------------------------------

def chunk_text(text: str, size: int = 500, overlap: int = 50) -> list[str]:
    text = re.sub(r"\r\n", "\n", text or "").strip()
    if not text:
        return []
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start:start + size])
        start += size - overlap
    return [c.strip() for c in chunks if c.strip()]


async def embed_texts(db: AsyncSession, texts: list[str]) -> list[list[float] | None]:
    """批量向量化；失败返回 [None]*n（降级关键词检索）。"""
    cfg = await _effective_cfg(db)
    url = cfg["base_url"].rstrip("/") + "/embeddings"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url,
                json={"model": cfg["embed_model"], "input": texts},
                headers=_headers(cfg["api_key"]),
            )
            resp.raise_for_status()
            data = sorted(resp.json().get("data", []), key=lambda d: d.get("index", 0))
            return [d.get("embedding") for d in data]
    except Exception as e:
        logger.warning("embed failed, fallback to keyword: %s", e)
        return [None] * len(texts)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _keyword_score(query: str, text: str) -> float:
    """简单关键词重合度：按 2-gram 切查询串，统计命中比例。"""
    q = re.sub(r"\s+", "", query or "")
    if len(q) < 2:
        return 0.0
    grams = {q[i:i + 2] for i in range(len(q) - 1)}
    hit = sum(1 for g in grams if g in text)
    return hit / len(grams)


async def kb_search(db: AsyncSession, query: str, top_k: int = 4) -> list[dict]:
    """混合检索：有向量用余弦（权重0.7）+关键词（0.3）；无向量纯关键词。"""
    q_vec = (await embed_texts(db, [query[:500]]))[0]
    rows = (await db.execute(
        select(AiKbChunk, AiKbDoc.filename).join(AiKbDoc, AiKbChunk.doc_id == AiKbDoc.id)
    )).all()
    scored = []
    for chunk, filename in rows:
        if q_vec and chunk.embedding:
            score = 0.7 * _cosine(q_vec, chunk.embedding) + 0.3 * _keyword_score(query, chunk.content)
        else:
            score = _keyword_score(query, chunk.content)
        if score > 0.05:
            scored.append({"filename": filename, "content": chunk.content, "score": round(score, 4)})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


async def kb_add_document(db: AsyncSession, filename: str, content: str, note: str = "") -> dict:
    """入库：切块 → 向量化（可失败）→ 落库。"""
    doc = AiKbDoc(filename=filename[:255], size=len(content.encode("utf-8")), status="processing", note=note[:255] or None)
    db.add(doc)
    await db.flush()
    chunks = chunk_text(content)
    if not chunks:
        doc.status = "failed"
        doc.note = "文档内容为空"
        await db.commit()
        return {"id": doc.id, "chunks": 0, "status": "failed"}
    vectors = await embed_texts(db, chunks)  # 耗时操作，逐条 flush 前完成
    for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
        db.add(AiKbChunk(doc_id=doc.id, seq=i, content=chunk, embedding=vec))
    doc.chunk_count = len(chunks)
    doc.status = "ready"
    await db.commit()
    return {"id": doc.id, "chunks": len(chunks), "status": "ready"}


async def kb_build_context(db: AsyncSession, query: str, max_chars: int = 1600) -> str:
    """检索知识库并拼接为可注入 prompt 的上下文块（超长截断）。"""
    try:
        hits = await kb_search(db, query)
    except Exception as e:
        logger.warning("kb context failed: %s", e)
        return ""
    if not hits:
        return ""
    parts, total = [], 0
    for h in hits:
        seg = f"【{h['filename']}】{h['content']}"
        if total + len(seg) > max_chars:
            break
        parts.append(seg)
        total += len(seg)
    return "\n---\n".join(parts)


# ---------------------------------------------------------------------------
# 上下文构造（从平台数据组装 prompt，含脱敏）
# ---------------------------------------------------------------------------

def _device_brief(d) -> str:
    return (
        f"- 名称: {d.name}  IP: {d.ip}  厂商: {d.vendor or '未知'}  型号: {d.model or '未知'}\n"
        f"- 类型: {d.device_type or '未知'}  状态: {d.status}  位置: {d.location or '-'}\n"
        f"- CPU: {d.cpu_usage if d.cpu_usage is not None else '-'}%  "
        f"内存: {d.memory_usage if d.memory_usage is not None else '-'}%  "
        f"温度: {d.temperature if d.temperature is not None else '-'}℃"
    )


async def collect_recent_alerts_text(db, device_id: int, exclude_id: int, limit: int = 5) -> str:
    from app.models.alert import Alert
    rows = (await db.execute(
        select(Alert).where(Alert.device_id == device_id, Alert.id != exclude_id)
        .order_by(Alert.id.desc()).limit(limit)
    )).scalars().all()
    if not rows:
        return "（无）"
    return "\n".join(f"- [{a.severity}] {a.rule_name}: {a.message} ({a.triggered_at})" for a in rows)


async def collect_alert_stats_text(db) -> str:
    """最近24h告警统计 + 告警明细 + 设备状态统计（供日报）。"""
    from sqlalchemy import func
    from app.models.alert import Alert
    from app.models.device import Device
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    sev_rows = (await db.execute(
        select(Alert.severity, func.count(Alert.id))
        .where(Alert.triggered_at >= since).group_by(Alert.severity)
    )).all()
    stat = "、".join(f"{sev_cn(s)}({s}) {c} 条" for s, c in sev_rows) or "0 条"
    top_rows = (await db.execute(
        select(Alert.device_id, func.count(Alert.id))
        .where(Alert.triggered_at >= since).group_by(Alert.device_id)
        .order_by(func.count(Alert.id).desc()).limit(5)
    )).all()
    dev_names = {}
    if top_rows:
        ids = [r[0] for r in top_rows if r[0]]
        if ids:
            for d in (await db.execute(select(Device).where(Device.id.in_(ids)))).scalars():
                dev_names[d.id] = f"{d.name}({d.ip})"
    top_text = "\n".join(
        f"- {dev_names.get(r[0], f'设备#{r[0]}')}: {r[1]} 条" for r in top_rows
    ) or "（无）"
    # 最近告警明细（含告警内容，供日报分析根因/影响）
    detail_rows = (await db.execute(
        select(Alert).where(Alert.triggered_at >= since)
        .order_by(Alert.triggered_at.desc()).limit(10)
    )).scalars().all()
    if detail_rows:
        dmap = {}
        for d in (await db.execute(select(Device).where(
                Device.id.in_({a.device_id for a in detail_rows if a.device_id})))).scalars():
            dmap[d.id] = f"{d.name}({d.ip})"
        detail_text = "\n".join(
            f"- [{sev_cn(a.severity)}] {dmap.get(a.device_id, f'设备#{a.device_id}')} — "
            f"{a.rule_name}: {a.message}（{a.triggered_at:%m-%d %H:%M}）"
            for a in detail_rows
        )
    else:
        detail_text = "（最近 24 小时无告警）"
    st_rows = (await db.execute(select(Device.status, func.count(Device.id)).group_by(Device.status))).all()
    st_text = "、".join(f"{'在线' if s == 'online' else '离线' if s == 'offline' else (s or '未知')} {c} 台" for s, c in st_rows) or "无设备"
    return (
        f"## 最近24小时告警分级统计\n{stat}\n\n"
        f"## 告警最多的设备 TOP5\n{top_text}\n\n"
        f"## 最近24小时告警明细（最新10条）\n{detail_text}\n\n"
        f"## 设备状态分布\n{st_text}\n\n"
        f"当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )


# 并发限制：本地小模型同时只跑一个推理，排队等待
_ai_semaphore = asyncio.Semaphore(2)
