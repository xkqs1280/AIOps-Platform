# -*- coding: utf-8 -*-
"""告警通知分发：统一出口，扇出到邮件与各 IM 群机器人。

背景：平台此前只有 SMTP 邮件一种告警出口，而国内客户的日常值守在群里——
钉钉/企业微信/飞书机器人是实际被看到的通道。本模块把「告警产生」与
「怎么发出去」解耦：告警引擎只需调用 dispatch_alert()。

支持的通道：
  - dingtalk 钉钉群机器人（支持加签 secret、@所有人）
  - wecom    企业微信群机器人
  - feishu   飞书群机器人
  - webhook  自定义 HTTP 回调（JSON 结构固定，便于对接自建系统）

可靠性：
  - 各通道并发发送，单通道失败不影响其它通道与告警主流程；
  - 按通道 retry_times 重试；
  - 复用与邮件一致的防轰炸窗口（同 dedup_key 同通道 5 分钟内只发一次）；
  - 发送结果回写 last_result / last_sent_at，前端能看到通道是否健康。
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notify_channel import CHANNEL_TYPES, SEVERITY_ORDER, NotifyChannel
from app.services.credential_service import protect_secret, reveal_secret

logger = logging.getLogger("aiops.notify")

# 防轰炸窗口（秒）：与 mail_service.DEBOUNCE_SECONDS 对齐
DEBOUNCE_SECONDS = 300
_last_sent: dict[str, float] = {}

HTTP_TIMEOUT = 10.0

# 各通道中文名（日志与前端提示用）
_TYPE_LABEL = {
    "dingtalk": "钉钉",
    "wecom": "企业微信",
    "feishu": "飞书",
    "webhook": "Webhook",
}


def _debounced(key: str) -> bool:
    now = time.monotonic()
    last = _last_sent.get(key)
    if last and now - last < DEBOUNCE_SECONDS:
        return False
    _last_sent[key] = now
    return True


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 12:
        return value[:2] + "****"
    return value[:6] + "****" + value[-4:]


def mask_webhook(url: str) -> str:
    """回显用的 webhook 掩码：隐藏 token/key 主体。"""
    if not url:
        return ""
    if "?" in url:
        base, _, query = url.partition("?")
        return f"{base}?{_mask(query)}"
    if "/" in url:
        base, _, tail = url.rpartition("/")
        return f"{base}/{_mask(tail)}"
    return _mask(url)


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------

def _channel_dict(row: NotifyChannel, reveal: bool = False) -> dict:
    url = reveal_secret(row.webhook_url) or ""
    secret = reveal_secret(row.secret) or ""
    return {
        "id": row.id,
        "name": row.name,
        "channel_type": row.channel_type,
        "webhook_url": url if reveal else mask_webhook(url),
        "secret": secret if reveal else (("****" if secret else "")),
        "enabled": row.enabled,
        "min_severity": row.min_severity,
        "mention_all": row.mention_all,
        "retry_times": row.retry_times,
        "last_result": row.last_result,
        "last_sent_at": row.last_sent_at.isoformat() if row.last_sent_at else None,
    }


async def list_channels(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(select(NotifyChannel).order_by(NotifyChannel.id))).scalars().all()
    return [_channel_dict(r) for r in rows]


async def save_channel(db: AsyncSession, data: dict, channel_id: int | None = None) -> dict:
    """新增或更新通道。

    合并语义：**只覆盖传入的字段**（PUT 走 exclude_unset），未传字段保持原值。
    新建时未传字段用默认值兜底。webhook_url/secret 仅在传入新值（非掩码回显
    "****"）时覆盖；显式传空串表示清空。
    """
    row = None
    if channel_id is not None:
        row = (await db.execute(
            select(NotifyChannel).where(NotifyChannel.id == channel_id)
        )).scalar_one_or_none()
    is_update = row is not None
    if row is None:
        row = NotifyChannel()
        db.add(row)

    def provided(key: str) -> bool:
        return key in data and data[key] is not None

    # 通道类型：新建必传（路由已强校验）；更新时不传则保持原类型
    if provided("channel_type"):
        ctype = str(data["channel_type"]).strip()
        if ctype not in CHANNEL_TYPES:
            raise ValueError(f"不支持的通道类型：{ctype}")
    else:
        ctype = (row.channel_type or "webhook") if is_update else "webhook"
    row.channel_type = ctype

    default_name = f"{_TYPE_LABEL.get(ctype, ctype)}通道"
    if provided("name"):
        row.name = str(data["name"]).strip() or default_name
    elif not is_update:
        row.name = default_name

    if provided("webhook_url"):
        url = str(data["webhook_url"]).strip()
        # 掩码回显（含 ****）视为未修改，不覆盖已存地址
        if url and "****" not in url:
            row.webhook_url = protect_secret(url) or ""
        elif url == "":
            row.webhook_url = ""

    if provided("secret"):
        secret = str(data["secret"]).strip()
        if secret and "****" not in secret:
            row.secret = protect_secret(secret) or ""
        elif secret == "":
            row.secret = ""

    if provided("enabled"):
        row.enabled = bool(data["enabled"])
    elif not is_update:
        row.enabled = True

    if provided("min_severity"):
        sev = str(data["min_severity"]).strip()
        row.min_severity = sev if sev in SEVERITY_ORDER else "warning"
    elif not is_update:
        row.min_severity = "warning"

    if provided("mention_all"):
        row.mention_all = bool(data["mention_all"])
    elif not is_update:
        row.mention_all = False

    if provided("retry_times"):
        try:
            row.retry_times = max(0, min(3, int(data["retry_times"])))
        except (TypeError, ValueError):
            row.retry_times = 1
    elif not is_update:
        row.retry_times = 1

    await db.commit()
    await db.refresh(row)
    return _channel_dict(row)


async def delete_channel(db: AsyncSession, channel_id: int) -> bool:
    row = (await db.execute(
        select(NotifyChannel).where(NotifyChannel.id == channel_id)
    )).scalar_one_or_none()
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True


# ---------------------------------------------------------------------------
# 发送实现
# ---------------------------------------------------------------------------

def _build_payload(row: NotifyChannel, title: str, text: str, severity: str) -> tuple[str, dict]:
    """按通道类型构造 (url, json_payload)。"""
    ctype = row.channel_type
    url = reveal_secret(row.webhook_url) or ""

    if ctype == "dingtalk":
        secret = reveal_secret(row.secret) or ""
        if secret:
            ts = str(round(time.time() * 1000))
            string_to_sign = f"{ts}\n{secret}"
            digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                              digestmod=hashlib.sha256).digest()
            sign = quote_plus(base64.b64encode(digest))
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}timestamp={ts}&sign={sign}"
        payload = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
        if row.mention_all:
            payload["at"] = {"isAtAll": True}
        return url, payload

    if ctype == "wecom":
        if row.mention_all:
            return url, {"msgtype": "text", "text": {"content": f"{title}\n{text}", "mentioned_list": ["@all"]}}
        return url, {"msgtype": "markdown", "markdown": {"content": f"**{title}**\n{text}"}}

    if ctype == "feishu":
        return url, {"msg_type": "text", "content": {"text": f"{title}\n{text}"}}

    # 自定义 webhook：固定结构，便于对接自建系统
    return url, {
        "source": "AIOps",
        "title": title,
        "text": text,
        "severity": severity,
        "timestamp": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
    }


async def _post(url: str, payload: dict) -> tuple[bool, str]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(url, json=payload)
    body = (resp.text or "")[:200]
    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code}: {body}"

    # 钉钉/企微/飞书即使参数错误也返回 200，需检查 errcode/code
    try:
        data = json.loads(resp.text or "{}")
    except ValueError:
        return True, "HTTP 200"
    for key in ("errcode", "code", "StatusCode"):
        if key in data:
            code = data.get(key)
            if code in (0, "0"):
                return True, "发送成功"
            return False, f"{key}={code} {data.get('errmsg') or data.get('msg') or ''}".strip()
    return True, "发送成功"


async def send_to_channel(row: NotifyChannel, title: str, text: str, severity: str) -> dict:
    """向单个通道发送（含重试）。返回 {sent, reason}。"""
    url = reveal_secret(row.webhook_url) or ""
    if not url:
        return {"sent": False, "reason": "未配置 webhook 地址"}

    label = _TYPE_LABEL.get(row.channel_type, row.channel_type)
    try:
        target_url, payload = _build_payload(row, title, text, severity)
    except Exception as e:
        return {"sent": False, "reason": f"{label} 构造请求失败：{str(e)[:120]}"}

    attempts = max(1, (row.retry_times or 0) + 1)
    last_reason = ""
    for i in range(attempts):
        try:
            ok, reason = await _post(target_url, payload)
            if ok:
                return {"sent": True, "reason": "发送成功"}
            last_reason = reason
        except Exception as e:
            last_reason = f"{type(e).__name__}: {str(e)[:120]}"
        if i < attempts - 1:
            await asyncio.sleep(1.0)
    logger.warning("%s 推送失败：%s", label, last_reason)
    return {"sent": False, "reason": last_reason}


async def test_channel(db: AsyncSession, channel_id: int) -> dict:
    """测试发送（绕过防轰炸窗口与级别过滤）。"""
    row = (await db.execute(
        select(NotifyChannel).where(NotifyChannel.id == channel_id)
    )).scalar_one_or_none()
    if row is None:
        return {"sent": False, "reason": "通道不存在"}

    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    title = "【测试】AIOps 告警通道连通性验证"
    text = (
        f"**AIOps 智能运维托管平台**\n"
        f"- 通道：{row.name}（{_TYPE_LABEL.get(row.channel_type, row.channel_type)}）\n"
        f"- 时间：{now}\n"
        f"- 说明：收到此消息说明该通道配置正确，告警将可正常推送。"
    )
    result = await send_to_channel(row, title, text, "minor")
    row.last_result = ("测试成功" if result["sent"] else f"测试失败：{result['reason']}")[:255]
    row.last_sent_at = datetime.now(timezone.utc)
    await db.commit()
    return result


# ---------------------------------------------------------------------------
# 统一分发入口
# ---------------------------------------------------------------------------

def _format_text(device_name: str, device_ip: str, rule_name: str,
                 severity: str, message: str) -> str:
    ts = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    sev_cn = {"critical": "严重", "major": "重要", "minor": "次要", "warning": "警告"}.get(severity, severity)
    return (
        f"**告警时间**：{ts}\n"
        f"**严重级别**：{sev_cn}\n"
        f"**设备名称**：{device_name}\n"
        f"**IP 地址**：{device_ip}\n"
        f"**规则名称**：{rule_name}\n"
        f"**告警内容**：{message}"
    )


async def dispatch_alert(
    db: AsyncSession,
    *,
    device_name: str,
    device_ip: str,
    rule_name: str,
    severity: str,
    message: str,
    dedup_key: str,
    notify: bool = True,
) -> dict:
    """统一告警出口：邮件 + 所有启用的 IM 通道。

    notify=False 时（告警被收敛抑制或判定为抖动）整条链路静默，
    但仍然返回结构，便于上层记录。
    """
    if not notify:
        return {"skipped": True, "reason": "告警已收敛，按策略不推送"}

    title = f"[{severity}] {rule_name} - {device_name}"
    text = _format_text(device_name, device_ip, rule_name, severity, message)

    # 邮件：沿用既有实现与防轰炸语义
    mail_result = {"sent": False, "reason": "未启用"}
    try:
        from app.services.mail_service import send_alert_email
        mail_result = await send_alert_email(
            db,
            subject=title,
            body=text.replace("**", ""),
            dedup_key=f"mail:{dedup_key}",
        )
    except Exception as e:
        logger.warning("dispatch mail failed: %s", e)
        mail_result = {"sent": False, "reason": str(e)[:120]}

    # IM 通道
    sev_rank = SEVERITY_ORDER.get((severity or "").lower(), 0)
    rows = (await db.execute(
        select(NotifyChannel).where(NotifyChannel.enabled.is_(True))
    )).scalars().all()

    targets = [
        r for r in rows
        if sev_rank >= SEVERITY_ORDER.get(r.min_severity or "warning", 1)
        and _debounced(f"chan:{r.id}:{dedup_key}")
    ]
    results: list[dict] = []
    if targets:
        sent = await asyncio.gather(
            *[send_to_channel(r, title, text, severity) for r in targets],
            return_exceptions=True,
        )
        now = datetime.now(timezone.utc)
        for r, res in zip(targets, sent):
            if isinstance(res, Exception):
                res = {"sent": False, "reason": str(res)[:120]}
            r.last_result = ("发送成功" if res["sent"] else f"失败：{res['reason']}")[:255]
            r.last_sent_at = now
            results.append({"channel": r.name, "type": r.channel_type, **res})
        try:
            await db.commit()
        except Exception as e:
            logger.warning("persist channel result failed: %s", e)

    return {"mail": mail_result, "channels": results}
