# -*- coding: utf-8 -*-
"""平台访问 IP 白名单回归测试。

背景：账号管理页新增「访问 IP 白名单」，管理员可自定义哪些来源 IP 能访问平台
（整站拦截，含前端页面）。功能本身简单，但**锁错一次就是全平台不可访问**，
所以这里把最容易改错的几条契约钉死：

  1. 默认关闭 —— 没开开关时任何 IP 都能访问，升级上来的老部署不会被新功能锁死；
  2. 整站拦截 —— 前端页面与 API 一起拦，且拒绝页不依赖任何外部静态资源
     （此刻静态资源同样被拦，依赖它只会给出一片空白）；
  3. 回环逃生 —— 127.0.0.1 / ::1 永远放行，这是误锁后唯一的自救通道；
  4. 只信「回环来的」转发头 —— 远端直连伪造 ``X-Forwarded-For`` 不能绕过白名单；
  5. 防自锁 —— 提交的名单不含自己当前 IP 时，保存必须被拒绝；
  6. 设备上报端点豁免 —— 拦了就是设备日志/Trap 静默中断，代价远大于收益。
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.main import app
from app.models.access_control import AccessControlSetting, IpWhitelistEntry
from app.models.audit import AuditLog
from app.models.user import User
from app.services import ip_whitelist
from app.services.auth_service import create_access_token, hash_password

WL_URL = "/api/v1/access-control/ip-whitelist"


@pytest.fixture(autouse=True)
def _no_stale_cache():
    """白名单状态是进程级 TTL 缓存，用例之间必须清干净，否则「上次生效的配置」会串味。"""
    reset = {"ts": 0.0, "enabled": False, "networks": (), "loaded": False}
    ip_whitelist._STATE.update(reset)
    yield
    ip_whitelist._STATE.update(dict(reset))


class _BrokenSession:
    """模拟数据库不可用（``async with async_session()`` 直接炸）。"""

    async def __aenter__(self):
        raise RuntimeError("database is down")

    async def __aexit__(self, *exc):
        return False


def _client(ip: str, token: str | None = None) -> AsyncClient:
    """造一个「TCP 对端 = ip」的测试客户端。"""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return AsyncClient(
        transport=ASGITransport(app=app, client=(ip, 51234)),
        base_url="http://test",
        headers=headers,
    )


async def _seed_user(db, username: str = "admin", role: str = "admin") -> None:
    db.add(User(username=username, password_hash=hash_password("Admin@123456"), role=role, is_active=True))
    await db.commit()


async def _seed_whitelist(db, *, enabled: bool, entries: list[tuple[str, str]] = ()) -> None:
    db.add(AccessControlSetting(ip_whitelist_enabled=enabled, updated_by="admin"))
    for cidr, remark in entries:
        db.add(IpWhitelistEntry(cidr=cidr, remark=remark, created_by="admin"))
    await db.commit()


# ---------------------------------------------------------------- 匹配逻辑


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("192.168.1.10", "192.168.1.10/32"),
        ("192.168.1.5/24", "192.168.1.0/24"),  # 用户习惯写法按网段规范化
        ("10.0.0.0/8", "10.0.0.0/8"),
        ("::1", "::1/128"),
    ],
)
def test_normalize_cidr(raw, expected):
    assert ip_whitelist.normalize_cidr(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "999.1.1.1", "192.168.1.0/33", "10.0.0.0/8/8"])
def test_normalize_cidr_rejects_garbage(raw):
    with pytest.raises(ValueError):
        ip_whitelist.normalize_cidr(raw)


def test_entries_dedup_and_match():
    entries = ip_whitelist.normalize_entries(
        [{"cidr": "10.1.1.0/24", "remark": "办公网"}, {"cidr": "10.1.1.0/24", "remark": "重复"}]
    )
    assert entries == [("10.1.1.0/24", "办公网")]

    networks = ip_whitelist.compile_networks(["10.1.1.0/24", "192.168.5.5/32"])
    assert ip_whitelist.ip_in_networks("10.1.1.77", networks) is True
    assert ip_whitelist.ip_in_networks("192.168.5.5", networks) is True
    assert ip_whitelist.ip_in_networks("10.1.2.1", networks) is False
    # 认不出来的来源一律不放行（fail-closed）
    assert ip_whitelist.ip_in_networks("", networks) is False
    assert ip_whitelist.ip_in_networks("not-an-ip", networks) is False


def test_loopback_detection():
    assert ip_whitelist.is_loopback_ip("127.0.0.1") is True
    assert ip_whitelist.is_loopback_ip("127.0.0.53") is True
    assert ip_whitelist.is_loopback_ip("::1") is True
    assert ip_whitelist.is_loopback_ip("10.0.0.1") is False
    assert ip_whitelist.is_loopback_ip("") is False


# ---------------------------------------------------------------- 中间件拦截


async def test_disabled_by_default_lets_everyone_in(db):
    """不开开关 = 不限制：这是升级安全性的底线。"""
    await _seed_user(db)
    token = create_access_token("admin", "admin")
    async with _client("8.8.8.8", token) as c:
        resp = await c.get("/api/v1/auth/me")
    assert resp.status_code == 200


async def test_enabled_blocks_ip_outside_whitelist(db):
    await _seed_user(db)
    await _seed_whitelist(db, enabled=True, entries=[("10.1.1.0/24", "办公网")])
    token = create_access_token("admin", "admin")

    async with _client("8.8.8.8", token) as c:
        blocked = await c.get("/api/v1/auth/me")
    assert blocked.status_code == 403
    assert "白名单" in blocked.json()["detail"]

    async with _client("10.1.1.5", token) as c:
        allowed = await c.get("/api/v1/auth/me")
    assert allowed.status_code == 200


async def test_frontend_page_blocked_with_self_contained_html(db):
    """整站拦截：前端页面同样被拦，且拒绝页不能依赖被拦掉的静态资源。"""
    await _seed_user(db)
    await _seed_whitelist(db, enabled=True, entries=[("10.1.1.0/24", "")])

    async with _client("8.8.8.8") as c:
        resp = await c.get("/dashboard")

    assert resp.status_code == 403
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "访问受限" in body
    assert "8.8.8.8" in body  # 要让用户看到自己的来源 IP，否则没法找管理员加白名单
    assert "<script" not in body.lower()
    assert "<link" not in body.lower()


async def test_loopback_always_allowed_is_escape_hatch(db):
    """回环永久放行：名单里没有 127.0.0.1 也要能进，否则误锁后无法自救。"""
    await _seed_user(db)
    await _seed_whitelist(db, enabled=True, entries=[("10.1.1.0/24", "")])
    token = create_access_token("admin", "admin")

    async with _client("127.0.0.1", token) as c:
        resp = await c.get("/api/v1/auth/me")
    assert resp.status_code == 200


async def test_forwarded_header_trusted_only_from_loopback(db):
    """转发头只在「请求来自回环」时采信，远端直连伪造无效。"""
    await _seed_user(db)
    await _seed_whitelist(db, enabled=True, entries=[("10.9.9.9/32", "")])
    token = create_access_token("admin", "admin")

    # 同机反代场景：回环 + XFF → 按 XFF 里的真实客户端判定 → 命中白名单
    async with _client("127.0.0.1", token) as c:
        proxied = await c.get("/api/v1/auth/me", headers={"X-Forwarded-For": "10.9.9.9"})
    assert proxied.status_code == 200

    # 远端直连伪造同样的头 → 必须仍被拦（否则白名单一行 header 就能绕过）
    async with _client("8.8.8.8", token) as c:
        forged = await c.get("/api/v1/auth/me", headers={"X-Forwarded-For": "10.9.9.9"})
    assert forged.status_code == 403
    assert "白名单" in forged.json()["detail"]


async def test_device_ingest_and_health_are_exempt(db):
    """设备上报端点与健康检查不能被拦：拦了就是日志/Trap 静默中断。"""
    await _seed_user(db)
    await _seed_whitelist(db, enabled=True, entries=[("10.1.1.0/24", "")])

    async with _client("172.16.0.9") as c:
        health = await c.get("/health")
        ingest = await c.post("/api/v1/device-logs/ingest", json={})

    assert health.status_code == 200
    # ingest 会在后续鉴权/限流处被挡住，但**绝不能**是白名单给的 403
    assert "白名单" not in ingest.text


async def test_dirty_db_entry_is_skipped_not_fatal(db):
    """库里混进一条非法条目时只跳过它，不能让整个平台变成拒绝服务。"""
    await _seed_user(db, username="admin")
    await _seed_whitelist(db, enabled=True, entries=[("10.1.1.0/24", "办公网")])
    db.add(IpWhitelistEntry(cidr="这不是网段", remark="历史脏数据", created_by="admin"))
    await db.commit()
    token = create_access_token("admin", "admin")

    async with _client("10.1.1.5", token) as c:
        allowed = await c.get("/api/v1/auth/me")
    assert allowed.status_code == 200

    async with _client("8.8.8.8", token) as c:
        blocked = await c.get("/api/v1/auth/me")
    assert blocked.status_code == 403


async def test_db_failure_keeps_last_known_policy(db, monkeypatch):
    """数据库抖动时必须沿用上次生效的配置。

    否则「把数据库打挂」就成了一条绕过白名单的路径：读不到名单就放行，
    等于白名单在最需要它的时候自动失效。
    """
    await _seed_user(db)
    await _seed_whitelist(db, enabled=True, entries=[("10.1.1.0/24", "")])
    token = create_access_token("admin", "admin")

    # 先正常读一次，让配置生效
    async with _client("8.8.8.8", token) as c:
        assert (await c.get("/api/v1/auth/me")).status_code == 403

    monkeypatch.setattr(ip_whitelist, "async_session", lambda: _BrokenSession())
    ip_whitelist.invalidate()

    async with _client("8.8.8.8", token) as c:
        still_blocked = await c.get("/api/v1/auth/me")
    assert still_blocked.status_code == 403
    async with _client("10.1.1.5", token) as c:
        still_allowed = await c.get("/api/v1/auth/me")
    assert still_allowed.status_code == 200


async def test_db_failure_on_cold_start_does_not_lock_platform(db, monkeypatch):
    """冷启动就读不到配置时不拦截任何人：新功能不该把平台变成谁也进不去。"""
    await _seed_user(db)
    await _seed_whitelist(db, enabled=True, entries=[("10.1.1.0/24", "")])
    token = create_access_token("admin", "admin")

    # 回到「进程刚起来、一次配置都没读到过」的状态
    ip_whitelist._STATE.update({"ts": 0.0, "enabled": False, "networks": (), "loaded": False})
    monkeypatch.setattr(ip_whitelist, "async_session", lambda: _BrokenSession())

    async with _client("8.8.8.8", token) as c:
        resp = await c.get("/api/v1/auth/me")
    assert resp.status_code == 200


# ---------------------------------------------------------------- 配置接口


async def test_put_saves_and_takes_effect_immediately(db):
    await _seed_user(db)
    token = create_access_token("admin", "admin")

    async with _client("8.8.8.8", token) as c:
        saved = await c.put(
            WL_URL,
            json={"enabled": True, "entries": [{"cidr": "8.8.8.0/24", "remark": "办公网"}]},
        )
    assert saved.status_code == 200, saved.text
    assert saved.json()["enabled"] is True
    assert saved.json()["entries"][0]["cidr"] == "8.8.8.0/24"

    # 保存后立即可见（不等 5 秒 TTL）：自己还在名单内 → 通
    async with _client("8.8.8.8", token) as c:
        assert (await c.get("/api/v1/auth/me")).status_code == 200
    async with _client("8.8.4.4", token) as c:
        assert (await c.get("/api/v1/auth/me")).status_code == 403

    row = (await db.execute(select(AccessControlSetting))).scalars().first()
    assert row is not None and row.ip_whitelist_enabled is True and row.updated_by == "admin"
    audits = (
        await db.execute(select(AuditLog).where(AuditLog.module == "access_control"))
    ).scalars().all()
    assert len(audits) == 1 and "启用" in audits[0].detail


async def test_put_rejects_self_lockout(db):
    """防自锁：提交的名单不含自己当前 IP 时必须拒绝，否则一保存就把自己关在门外。"""
    await _seed_user(db)
    token = create_access_token("admin", "admin")

    async with _client("8.8.8.8", token) as c:
        resp = await c.put(
            WL_URL,
            json={"enabled": True, "entries": [{"cidr": "10.1.1.0/24", "remark": "办公网"}]},
        )
    assert resp.status_code == 400
    assert "无法访问平台" in resp.json()["detail"]

    row = (await db.execute(select(AccessControlSetting))).scalars().first()
    assert row is None, "校验失败不得落库"
    assert (await db.execute(select(IpWhitelistEntry))).scalars().first() is None


async def test_put_rejects_enable_without_entries(db):
    await _seed_user(db)
    token = create_access_token("admin", "admin")
    async with _client("8.8.8.8", token) as c:
        resp = await c.put(WL_URL, json={"enabled": True, "entries": []})
    assert resp.status_code == 400
    assert "至少添加一条" in resp.json()["detail"]


async def test_put_rejects_invalid_cidr(db):
    await _seed_user(db)
    token = create_access_token("admin", "admin")
    async with _client("8.8.8.8", token) as c:
        resp = await c.put(
            WL_URL, json={"enabled": True, "entries": [{"cidr": "999.1.1.1", "remark": ""}]}
        )
    assert resp.status_code == 400
    assert "不是合法的 IP 或网段" in resp.json()["detail"]


async def test_save_replaces_entries_wholesale(db):
    """全量替换：请求体是权威列表，不能留下删不掉的幽灵条目。"""
    await _seed_user(db)
    await _seed_whitelist(db, enabled=False, entries=[("10.1.1.0/24", "旧条目"), ("10.2.2.0/24", "旧条目")])
    token = create_access_token("admin", "admin")

    async with _client("8.8.8.8", token) as c:
        resp = await c.put(
            WL_URL, json={"enabled": False, "entries": [{"cidr": "8.8.8.8", "remark": "新条目"}]}
        )
    assert resp.status_code == 200
    rows = (await db.execute(select(IpWhitelistEntry))).scalars().all()
    assert [r.cidr for r in rows] == ["8.8.8.8/32"]


async def test_close_whitelist_restores_access(db):
    """关闭开关后立即恢复访问（含不在名单的 IP）。"""
    await _seed_user(db)
    await _seed_whitelist(db, enabled=True, entries=[("10.1.1.0/24", "")])
    token = create_access_token("admin", "admin")

    async with _client("10.1.1.5", token) as c:
        resp = await c.put(
            WL_URL, json={"enabled": False, "entries": [{"cidr": "10.1.1.0/24", "remark": ""}]}
        )
    assert resp.status_code == 200

    async with _client("8.8.8.8", token) as c:
        assert (await c.get("/api/v1/auth/me")).status_code == 200


async def test_read_endpoint_returns_current_ip(db):
    """界面要显示「平台看到的您当前来源 IP」，管理员才不用去猜自己的出口 IP。"""
    await _seed_user(db)
    token = create_access_token("admin", "admin")
    async with _client("8.8.8.8", token) as c:
        resp = await c.get(WL_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False and body["entries"] == [] and body["current_ip"] == "8.8.8.8"


async def test_whitelist_admin_only(db):
    """viewer 不得读取或修改白名单（配置读接口也只对管理员开放）。"""
    await _seed_user(db, username="admin", role="admin")
    await _seed_user(db, username="watcher", role="viewer")
    token = create_access_token("watcher", "viewer")

    async with _client("8.8.8.8", token) as c:
        assert (await c.get(WL_URL)).status_code == 403
        put = await c.put(WL_URL, json={"enabled": False, "entries": []})
    assert put.status_code == 403
