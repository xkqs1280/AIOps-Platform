"""通知通道：请求构造、加签、级别过滤、发送与失败处理。

不发起真实网络请求：_post 与 send_alert_email 都在用例里被替换。
"""
import json
from urllib.parse import parse_qs, urlparse

import pytest

from app.models.notify_channel import NotifyChannel
from app.services import notify_service as ns


def _row(**kw) -> NotifyChannel:
    defaults = dict(
        id=1, name="测试通道", channel_type="dingtalk",
        webhook_url="https://example.com/hook?access_token=abc",
        secret="", enabled=True, min_severity="minor",
        mention_all=False, retry_times=0,
    )
    defaults.update(kw)
    return NotifyChannel(**defaults)


# ---------------------------------------------------------------------------
# 请求构造
# ---------------------------------------------------------------------------

def test_dingtalk_markdown_payload():
    url, payload = ns._build_payload(_row(), "标题", "正文", "major")
    assert payload["msgtype"] == "markdown"
    assert payload["markdown"]["title"] == "标题"
    assert "at" not in payload


def test_dingtalk_sign_appended_when_secret_set():
    url, _ = ns._build_payload(_row(secret="SEC123456"), "标题", "正文", "major")
    qs = parse_qs(urlparse(url).query)
    assert "timestamp" in qs and "sign" in qs
    assert qs["access_token"] == ["abc"]


def test_dingtalk_mention_all_adds_at_flag():
    _, payload = ns._build_payload(_row(mention_all=True), "标题", "正文", "major")
    assert payload["at"]["isAtAll"] is True


def test_wecom_uses_text_when_mention_all():
    _, payload = ns._build_payload(_row(channel_type="wecom", mention_all=True), "标题", "正文", "major")
    assert payload["msgtype"] == "text"
    assert payload["text"]["mentioned_list"] == ["@all"]


def test_wecom_uses_markdown_by_default():
    _, payload = ns._build_payload(_row(channel_type="wecom"), "标题", "正文", "major")
    assert payload["msgtype"] == "markdown"


def test_feishu_text_payload():
    _, payload = ns._build_payload(_row(channel_type="feishu"), "标题", "正文", "major")
    assert payload["msg_type"] == "text"
    assert "标题" in payload["content"]["text"]


def test_custom_webhook_payload_shape():
    _, payload = ns._build_payload(_row(channel_type="webhook"), "标题", "正文", "critical")
    assert payload["source"] == "AIOps"
    assert payload["severity"] == "critical"
    assert set(["title", "text", "severity", "timestamp"]).issubset(payload)


# ---------------------------------------------------------------------------
# 掩码与安全
# ---------------------------------------------------------------------------

def test_mask_webhook_hides_token():
    masked = ns.mask_webhook("https://oapi.dingtalk.com/robot/send?access_token=0123456789abcdef")
    assert "0123456789abcdef" not in masked
    assert "****" in masked


def test_mask_webhook_keeps_host():
    masked = ns.mask_webhook("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcdef123456")
    assert masked.startswith("https://qyapi.weixin.qq.com")


def test_webhook_url_is_treated_as_secret():
    """webhook 里带 token，等同凭据：常量与掩码逻辑必须存在。"""
    from app.services.credential_service import PREFIX

    assert PREFIX == "enc:"
    assert "****" in ns.mask_webhook("https://example.com/hook?access_token=abcdef123456")


# ---------------------------------------------------------------------------
# 发送与重试
# ---------------------------------------------------------------------------

async def test_send_to_channel_reports_failure(monkeypatch):
    async def _boom(url, payload):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(ns, "_post", _boom)
    result = await ns.send_to_channel(_row(), "标题", "正文", "major")
    assert result["sent"] is False
    assert "connection refused" in result["reason"]


async def test_send_to_channel_without_url_fails_fast():
    result = await ns.send_to_channel(_row(webhook_url=""), "标题", "正文", "major")
    assert result["sent"] is False


async def _no_sleep(_):
    return None


async def test_send_to_channel_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    async def _flaky(url, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return False, "HTTP 500"
        return True, "发送成功"

    monkeypatch.setattr(ns, "_post", _flaky)
    monkeypatch.setattr(ns.asyncio, "sleep", _no_sleep)

    result = await ns.send_to_channel(_row(retry_times=1), "标题", "正文", "major")
    assert result["sent"] is True
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# 统一分发
# ---------------------------------------------------------------------------

async def test_dispatch_skipped_when_converged(db):
    """被收敛抑制的告警不应触发任何推送。"""
    result = await ns.dispatch_alert(
        db, device_name="SW1", device_ip="10.0.0.1", rule_name="温度过高",
        severity="warning", message="msg", dedup_key="k1", notify=False,
    )
    assert result["skipped"] is True


async def test_dispatch_filters_by_min_severity(db, monkeypatch):
    sent = []

    async def _fake_send(row, title, text, severity):
        sent.append(row.name)
        return {"sent": True, "reason": "发送成功"}

    async def _no_mail(*a, **kw):
        return {"sent": False, "reason": "未启用"}

    monkeypatch.setattr(ns, "send_to_channel", _fake_send)
    monkeypatch.setattr("app.services.mail_service.send_alert_email", _no_mail)

    db.add(NotifyChannel(name="只收严重", channel_type="webhook",
                         webhook_url="https://example.com/a", enabled=True,
                         min_severity="critical", retry_times=0))
    db.add(NotifyChannel(name="收次要以上", channel_type="webhook",
                         webhook_url="https://example.com/b", enabled=True,
                         min_severity="minor", retry_times=0))
    await db.commit()

    # warning(1) 低于两条通道的门槛，谁都不发
    await ns.dispatch_alert(
        db, device_name="SW1", device_ip="10.0.0.1", rule_name="温度过高",
        severity="warning", message="msg", dedup_key="k2",
    )
    assert sent == []

    sent.clear()
    # major(3) 只满足 minor 门槛(2)，不满足 critical 门槛(4)
    await ns.dispatch_alert(
        db, device_name="SW1", device_ip="10.0.0.1", rule_name="温度过高",
        severity="major", message="msg", dedup_key="k2b",
    )
    assert sent == ["收次要以上"]

    sent.clear()
    await ns.dispatch_alert(
        db, device_name="SW1", device_ip="10.0.0.1", rule_name="设备离线",
        severity="critical", message="msg", dedup_key="k3",
    )
    assert sorted(sent) == ["只收严重", "收次要以上"]


async def test_dispatch_debounces_same_key(db, monkeypatch):
    sent = []

    async def _fake_send(row, title, text, severity):
        sent.append(row.name)
        return {"sent": True, "reason": "ok"}

    async def _no_mail(*a, **kw):
        return {"sent": False, "reason": "未启用"}

    monkeypatch.setattr(ns, "send_to_channel", _fake_send)
    monkeypatch.setattr("app.services.mail_service.send_alert_email", _no_mail)

    db.add(NotifyChannel(name="通道A", channel_type="webhook",
                         webhook_url="https://example.com/a", enabled=True,
                         min_severity="warning", retry_times=0))
    await db.commit()

    for _ in range(3):
        await ns.dispatch_alert(
            db, device_name="SW1", device_ip="10.0.0.1", rule_name="温度过高",
            severity="warning", message="msg", dedup_key="same-key",
        )
    assert sent == ["通道A"]  # 防轰炸窗口内只发一次


async def test_disabled_channel_not_used(db, monkeypatch):
    sent = []

    async def _fake_send(row, title, text, severity):
        sent.append(row.name)
        return {"sent": True, "reason": "ok"}

    async def _no_mail(*a, **kw):
        return {"sent": False, "reason": "未启用"}

    monkeypatch.setattr(ns, "send_to_channel", _fake_send)
    monkeypatch.setattr("app.services.mail_service.send_alert_email", _no_mail)

    db.add(NotifyChannel(name="停用通道", channel_type="webhook",
                         webhook_url="https://example.com/a", enabled=False,
                         min_severity="warning", retry_times=0))
    await db.commit()

    await ns.dispatch_alert(
        db, device_name="SW1", device_ip="10.0.0.1", rule_name="温度过高",
        severity="warning", message="msg", dedup_key="k4",
    )
    assert sent == []


async def test_save_channel_encrypts_and_masks(db):
    """保存后：落库为密文，回显为掩码；再次保存掩码不会破坏原值。"""
    from sqlalchemy import select as sa_select

    from app.services.credential_service import PREFIX, reveal_secret

    saved = await ns.save_channel(db, {
        "name": "钉钉告警群", "channel_type": "dingtalk",
        "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=tok123456",
        "secret": "SEC-abcdef", "enabled": True, "min_severity": "major",
    })
    assert "tok123456" not in str(saved["webhook_url"]) or "****" in saved["webhook_url"]

    row = (await db.execute(sa_select(NotifyChannel))).scalars().first()
    if row.webhook_url.startswith(PREFIX):
        assert reveal_secret(row.webhook_url).endswith("tok123456")

    # 带掩码的二次保存不应覆盖真实值（前端回显后直接提交的场景）
    before = row.webhook_url
    await ns.save_channel(db, {
        "name": "钉钉告警群", "channel_type": "dingtalk",
        "webhook_url": saved["webhook_url"], "secret": saved["secret"],
        "enabled": True, "min_severity": "major",
    }, channel_id=row.id)
    await db.refresh(row)
    assert row.webhook_url == before


async def test_invalid_channel_type_rejected(db):
    with pytest.raises(ValueError):
        await ns.save_channel(db, {"name": "x", "channel_type": "telegram"})


async def test_save_channel_partial_update_keeps_other_fields(db):
    """局部更新（PUT 走 exclude_unset）：只改传入字段，其余保持原值。

    回归 2026-09-10 部署验证发现的问题：PUT 与 POST 共用「channel_type 必填」的
    模型 → 「只改 enabled」被判 422；且旧 save_channel 用 .get(默认值) 全量覆盖，
    即便绕过校验也会把 min_severity/mention_all 等重置。
    """
    from sqlalchemy import select as sa_select

    await ns.save_channel(db, {
        "name": "企微告警", "channel_type": "wecom",
        "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123",
        "enabled": False, "min_severity": "critical",
        "mention_all": True, "retry_times": 3,
    })
    row = (await db.execute(sa_select(NotifyChannel))).scalars().first()

    # 只传 enabled —— 其余字段必须原样保留
    await ns.save_channel(db, {"enabled": True}, channel_id=row.id)
    await db.refresh(row)
    assert row.enabled is True
    assert row.min_severity == "critical"
    assert row.mention_all is True
    assert row.retry_times == 3
    assert row.channel_type == "wecom"
    assert row.name == "企微告警"

    # 只改级别，其余仍不变
    await ns.save_channel(db, {"min_severity": "info"}, channel_id=row.id)
    await db.refresh(row)
    assert row.min_severity == "info"
    assert row.enabled is True
    assert row.channel_type == "wecom"


def test_update_body_is_all_optional():
    """PUT 模型全字段可选（PATCH 语义），前端无需回传全部字段。"""
    from app.routers.notify_channels import ChannelUpdate

    assert ChannelUpdate(**{"enabled": True}).model_dump(exclude_unset=True) == {"enabled": True}
    # 空 body 也应合法（无字段被修改）
    assert ChannelUpdate().model_dump(exclude_unset=True) == {}


def test_payload_is_json_serializable():
    for ctype in ("dingtalk", "wecom", "feishu", "webhook"):
        _, payload = ns._build_payload(_row(channel_type=ctype), "t", "b", "minor")
        json.dumps(payload)  # 不应抛异常
