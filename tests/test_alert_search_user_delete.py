# -*- coding: utf-8 -*-
"""告警搜索 / 跳页所依赖的后端过滤，以及账号删除的守卫。

覆盖两类真实事故面：
  1. 搜索关键字里的 `%` / `_` 若不通配转义，用户搜「95%」会把全部告警带出来；
  2. 账号删除若不做守卫，可能删掉内置 admin 或最后一个管理员，把平台锁死。
"""
import pytest
from fastapi import HTTPException

from app.models.alert import Alert
from app.models.user import User
from app.routers.alerts import _like_pattern, list_alerts
from app.routers.auth import delete_user


# ---------------------------------------------------------------------------
# 关键字 → LIKE 模式
# ---------------------------------------------------------------------------

def test_like_pattern_escapes_wildcards():
    assert _like_pattern("100%") == "%100\\%%"
    assert _like_pattern("a_b") == "%a\\_b%"
    # 反斜杠本身也要先转义，否则 escape 字符会被吃掉
    assert _like_pattern("c\\d") == "%c\\\\d%"


def test_like_pattern_keeps_plain_text():
    assert _like_pattern("CPU 过高") == "%CPU 过高%"


# ---------------------------------------------------------------------------
# 告警搜索
# ---------------------------------------------------------------------------

async def _alert(db, device, message="通用告警", rule_name="CPU 过高",
                 severity="major", status="active"):
    a = Alert(device_id=device.id, rule_name=rule_name, severity=severity,
              message=message, status=status)
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


async def _search(db, **kw):
    params = dict(page=1, page_size=20, severity=None, status=None, device_id=None, q=None)
    params.update(kw)
    return await list_alerts(db=db, **params)


async def test_no_keyword_returns_all(db, device_factory):
    dev = await device_factory(name="SW1", ip="10.0.0.1")
    await _alert(db, dev, message="端口 GE1/0/1 down")
    await _alert(db, dev, message="CPU 使用率 92%")

    res = await _search(db)
    assert res.total == 2


async def test_blank_keyword_is_ignored(db, device_factory):
    dev = await device_factory(name="SW1", ip="10.0.0.1")
    await _alert(db, dev)

    assert (await _search(db, q="")).total == 1
    assert (await _search(db, q="   ")).total == 1


async def test_search_by_message(db, device_factory):
    dev = await device_factory(name="SW1", ip="10.0.0.1")
    hit = await _alert(db, dev, message="端口 GE1/0/1 down")
    await _alert(db, dev, message="CPU 使用率 92%")

    res = await _search(db, q="GE1/0/1")
    assert res.total == 1
    assert [i.id for i in res.items] == [hit.id]


async def test_search_by_rule_name(db, device_factory):
    dev = await device_factory(name="SW1", ip="10.0.0.1")
    await _alert(db, dev, rule_name="内存过高")
    hit = await _alert(db, dev, rule_name="接口流量超限")

    res = await _search(db, q="流量")
    assert res.total == 1
    assert [i.id for i in res.items] == [hit.id]


async def test_search_by_device_name_and_ip(db, device_factory):
    sw1 = await device_factory(name="核心交换机", ip="10.0.0.1")
    sw2 = await device_factory(name="接入交换机", ip="10.0.0.2")
    a1 = await _alert(db, sw1, message="A")
    a2 = await _alert(db, sw2, message="B")

    by_name = await _search(db, q="核心")
    assert by_name.total == 1
    assert [i.id for i in by_name.items] == [a1.id]
    assert by_name.items[0].device_name == "核心交换机"

    by_ip = await _search(db, q="10.0.0.2")
    assert by_ip.total == 1
    assert [i.id for i in by_ip.items] == [a2.id]


async def test_search_case_insensitive(db, device_factory):
    dev = await device_factory(name="SW1", ip="10.0.0.1")
    await _alert(db, dev, message="INTERFACE Gi0/1 DOWN")

    assert (await _search(db, q="interface")).total == 1
    assert (await _search(db, q="down")).total == 1


async def test_search_keyword_with_percent_matches_literally(db, device_factory):
    """搜「95%」不能被当成通配符把全部告警捞出来。"""
    dev = await device_factory(name="SW1", ip="10.0.0.1")
    hit = await _alert(db, dev, message="CPU 使用率 95% 持续 5 分钟")
    await _alert(db, dev, message="端口 GE1/0/1 down")

    res = await _search(db, q="95%")
    assert res.total == 1
    assert [i.id for i in res.items] == [hit.id]


async def test_search_underscore_matches_literally(db, device_factory):
    """下划线同理：搜 `_` 只能命中含下划线的内容。"""
    dev = await device_factory(name="SW1", ip="10.0.0.1")
    hit = await _alert(db, dev, message="接口 Gi0_1 异常")
    await _alert(db, dev, message="普通告警")

    res = await _search(db, q="_")
    assert res.total == 1
    assert [i.id for i in res.items] == [hit.id]


async def test_search_combines_with_severity_and_status(db, device_factory):
    dev = await device_factory(name="SW1", ip="10.0.0.1")
    target = await _alert(db, dev, message="CPU 过高", severity="critical", status="active")
    await _alert(db, dev, message="CPU 过高", severity="minor", status="active")
    await _alert(db, dev, message="CPU 过高", severity="critical", status="resolved")

    res = await _search(db, q="CPU", severity="critical", status="active")
    assert res.total == 1
    assert [i.id for i in res.items] == [target.id]


async def test_search_matches_nothing(db, device_factory):
    dev = await device_factory(name="SW1", ip="10.0.0.1")
    await _alert(db, dev, message="端口 down")

    res = await _search(db, q="不存在的关键字")
    assert res.total == 0
    assert res.items == []


async def test_search_pagination_keeps_total_filtered(db, device_factory):
    """分页 total 必须是过滤后的总数，否则前端页数会算错。"""
    dev = await device_factory(name="SW1", ip="10.0.0.1")
    for i in range(5):
        await _alert(db, dev, message=f"CPU 过高 #{i}")
    await _alert(db, dev, message="端口 down", rule_name="端口 Down")

    res = await _search(db, q="CPU", page=1, page_size=2)
    assert res.total == 5
    assert len(res.items) == 2

    last = await _search(db, q="CPU", page=3, page_size=2)
    assert last.total == 5
    assert len(last.items) == 1


# ---------------------------------------------------------------------------
# 账号删除
# ---------------------------------------------------------------------------

async def _user(db, username, role="viewer", is_active=True):
    u = User(username=username, password_hash="x", role=role, is_active=is_active)
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


async def _count(db):
    from sqlalchemy import func, select
    return (await db.execute(select(func.count(User.id)))).scalar()


def _actor(username="admin"):
    return {"sub": username, "role": "admin"}


async def test_delete_user_removes_account(db):
    admin = await _user(db, "admin", role="admin")
    victim = await _user(db, "ops1", role="operator")

    await delete_user(victim.id, actor=_actor(), db=db)

    assert (await db.get(User, victim.id)) is None
    assert (await db.get(User, admin.id)) is not None
    assert await _count(db) == 1


async def test_delete_builtin_admin_is_rejected(db):
    admin = await _user(db, "admin", role="admin")

    with pytest.raises(HTTPException) as e:
        await delete_user(admin.id, actor=_actor("someone"), db=db)
    assert e.value.status_code == 400
    assert "admin" in e.value.detail
    assert (await db.get(User, admin.id)) is not None


async def test_delete_self_is_rejected(db):
    admin = await _user(db, "admin", role="admin")

    with pytest.raises(HTTPException) as e:
        await delete_user(admin.id, actor=_actor("admin"), db=db)
    assert e.value.status_code == 400
    assert (await db.get(User, admin.id)) is not None


async def test_delete_last_admin_is_rejected(db):
    """没有任何内置 admin 时，删掉唯一管理员会把平台锁死。"""
    boss = await _user(db, "boss", role="admin")
    await _user(db, "ops1", role="operator")

    with pytest.raises(HTTPException) as e:
        await delete_user(boss.id, actor=_actor("ops1"), db=db)
    assert e.value.status_code == 400
    assert "管理员" in e.value.detail
    assert (await db.get(User, boss.id)) is not None


async def test_delete_admin_role_allowed_when_another_admin_exists(db):
    await _user(db, "admin", role="admin")
    extra = await _user(db, "boss2", role="admin")

    await delete_user(extra.id, actor=_actor(), db=db)
    assert (await db.get(User, extra.id)) is None


async def test_delete_unknown_user_returns_404(db):
    with pytest.raises(HTTPException) as e:
        await delete_user(999999, actor=_actor(), db=db)
    assert e.value.status_code == 404


async def test_deleted_user_token_becomes_invalid(db, device_factory):
    """删除后旧 token 必须失效（current_user 从库里读账号）。"""
    from app.routers.auth import current_user
    from app.services.auth_service import create_access_token

    await _user(db, "admin", role="admin")
    victim = await _user(db, "ops1", role="operator")
    token = create_access_token("ops1", "operator")
    assert (await current_user(access_token=token, authorization=None, db=db))["sub"] == "ops1"

    await delete_user(victim.id, actor=_actor(), db=db)

    with pytest.raises(HTTPException) as e:
        await current_user(access_token=token, authorization=None, db=db)
    assert e.value.status_code == 401
