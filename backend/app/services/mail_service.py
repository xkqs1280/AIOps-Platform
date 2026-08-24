# -*- coding: utf-8 -*-
"""邮件告警服务：SMTP 配置读写、异步发送告警/恢复邮件（带防轰炸窗口）。"""
import asyncio
import logging
import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from time import monotonic

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mail_setting import MailSetting

logger = logging.getLogger("aiops.mail")

# 防轰炸窗口：同一设备同一规则 5 分钟内最多发一次
DEBOUNCE_SECONDS = 300
# 内存级最近发送时间 {key: timestamp}
_last_sent: dict[str, float] = {}


def _mask_password(pwd: str) -> str:
    if not pwd:
        return ""
    return pwd if len(pwd) <= 4 else pwd[:2] + "*" * (len(pwd) - 4) + pwd[-2:]


async def get_mail_setting(db: AsyncSession) -> dict | None:
    row = (await db.execute(select(MailSetting))).scalars().first()
    if not row:
        return None
    return {
        "enabled": row.enabled,
        "smtp_host": row.smtp_host,
        "smtp_port": row.smtp_port,
        "smtp_user": row.smtp_user,
        "smtp_password": _mask_password(row.smtp_password),
        "use_ssl": row.use_ssl,
        "sender": row.sender,
        "recipients": row.recipients,
    }


async def save_mail_setting(db: AsyncSession, data: dict) -> dict:
    row = (await db.execute(select(MailSetting))).scalars().first()
    if not row:
        row = MailSetting()
        db.add(row)
    row.enabled = bool(data.get("enabled", False))
    row.smtp_host = (data.get("smtp_host") or "").strip()
    row.smtp_port = int(data.get("smtp_port") or 465)
    row.smtp_user = (data.get("smtp_user") or "").strip()
    # 密码仅在前端填写了新值（非空）时更新；留空表示保持原密码
    new_pwd = (data.get("smtp_password") or "").strip()
    if new_pwd:
        row.smtp_password = new_pwd
    row.use_ssl = bool(data.get("use_ssl", True))
    row.sender = (data.get("sender") or "").strip()
    row.recipients = (data.get("recipients") or "").strip()
    await db.commit()
    return await get_mail_setting(db)


def _debounced(key: str) -> bool:
    """返回 True 表示应发送；False 表示在防轰炸窗口内应跳过。"""
    now = monotonic()
    last = _last_sent.get(key)
    if last and now - last < DEBOUNCE_SECONDS:
        return False
    _last_sent[key] = now
    return True


def _send_sync(cfg: dict, subject: str, body: str) -> None:
    """同步发送（在 executor 线程中执行，避免阻塞事件循环）。"""
    host = cfg["smtp_host"]
    port = int(cfg["smtp_port"] or 465)
    user = cfg["smtp_user"] or ""
    pwd = cfg["smtp_password"] or ""
    sender = cfg["sender"] or (user if user else "aiops@localhost")
    recipients = [r.strip() for r in (cfg["recipients"] or "").split(",") if r.strip()]
    if not recipients:
        raise ValueError("未配置收件人")
    if not user and not pwd:
        # 部分内网 SMTP 免认证
        pass
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("AIOps 告警", sender))
    msg["To"] = ", ".join(recipients)
    if cfg.get("use_ssl"):
        with smtplib.SMTP_SSL(host, port, timeout=15) as s:
            if user:
                s.login(user, pwd)
            s.sendmail(sender, recipients, msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=15) as s:
            if cfg.get("use_starttls"):
                s.starttls()
            if user:
                s.login(user, pwd)
            s.sendmail(sender, recipients, msg.as_string())


async def send_alert_email(db: AsyncSession, subject: str, body: str, dedup_key: str | None = None) -> dict:
    """发送告警邮件。dedup_key 非空时按 5 分钟窗口去重。失败仅记日志不抛错。"""
    cfg_row = (await db.execute(select(MailSetting))).scalars().first()
    if not cfg_row or not cfg_row.enabled or not cfg_row.recipients:
        return {"sent": False, "reason": "邮件告警未启用或未配置"}
    if dedup_key and not _debounced(dedup_key):
        return {"sent": False, "reason": "防轰炸窗口内已发送过"}

    cfg = {
        "smtp_host": cfg_row.smtp_host,
        "smtp_port": cfg_row.smtp_port,
        "smtp_user": cfg_row.smtp_user,
        "smtp_password": cfg_row.smtp_password,
        "use_ssl": cfg_row.use_ssl,
        "use_starttls": not cfg_row.use_ssl,
        "sender": cfg_row.sender,
        "recipients": cfg_row.recipients,
    }
    try:
        await asyncio.to_thread(_send_sync, cfg, subject, body)
        return {"sent": True}
    except Exception as e:
        logger.warning("send alert email failed: %s", e)
        return {"sent": False, "reason": str(e)[:200]}
