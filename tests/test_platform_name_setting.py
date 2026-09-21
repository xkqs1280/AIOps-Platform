# -*- coding: utf-8 -*-
"""平台名称（监控大屏标题）可自定义 —— 回归测试。

背景：大屏标题原先写死在 Dashboard.vue 里（「AIOps 网络监控平台」）。平台是交付
给客户现场用的，代理商需要换成「某某单位网络监控平台」，写死意味着每次改名都要
改代码重新发版。

契约：
  1. 未自定义时返回空串（由前端回退到内置默认文案，后端不固化默认值）；
  2. 保存时两端空白要 strip，存空串 = 恢复默认；
  3. 单行表：反复保存不会每次都插一行（否则读取时要靠「取第一条」的运气）；
  4. 保存要留审计（模块 settings）。
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.models.audit import AuditLog
from app.models.platform_setting import PlatformSetting
from app.routers.auth import admin_only
from app.routers.settings import (
    PlatformSettingRequest,
    read_platform_setting,
    update_platform_setting,
)

ADMIN = {"sub": "admin", "role": "admin"}


async def test_empty_before_customized(db):
    """默认：没有任何行时返回空串，不返回内置文案。"""
    out = await read_platform_setting(db, ADMIN)
    assert out["site_name"] == ""
    assert out["updated_by"] == ""


async def test_save_and_read_back(db):
    """保存后能读回，且记录修改人。"""
    saved = await update_platform_setting(
        PlatformSettingRequest(site_name="某某单位网络监控平台"), db, ADMIN
    )
    assert saved["site_name"] == "某某单位网络监控平台"
    assert saved["updated_by"] == "admin"

    out = await read_platform_setting(db, ADMIN)
    assert out["site_name"] == "某某单位网络监控平台"
    assert out["updated_at"] is not None


async def test_whitespace_stripped_and_can_reset(db):
    """两端空白去除；传空串表示恢复默认（标题回落到内置文案）。"""
    await update_platform_setting(
        PlatformSettingRequest(site_name="  杭州某某科技有限公司网络监控平台  "), db, ADMIN
    )
    assert (await read_platform_setting(db, ADMIN))["site_name"] == "杭州某某科技有限公司网络监控平台"

    await update_platform_setting(PlatformSettingRequest(site_name="   "), db, ADMIN)
    assert (await read_platform_setting(db, ADMIN))["site_name"] == ""


async def test_single_row_upsert(db):
    """反复保存只维护一行，不是每次插入新行。"""
    for name in ("名称一", "名称二", "名称三"):
        await update_platform_setting(PlatformSettingRequest(site_name=name), db, ADMIN)

    count = (await db.execute(select(func.count(PlatformSetting.id)))).scalar()
    assert count == 1
    assert (await read_platform_setting(db, ADMIN))["site_name"] == "名称三"


async def test_save_writes_audit_log(db):
    """保存要留审计（module=settings），便于事后追溯是谁改的。"""
    await update_platform_setting(PlatformSettingRequest(site_name="某某单位网络监控平台"), db, ADMIN)

    rows = (await db.execute(select(AuditLog).where(AuditLog.module == "settings"))).scalars().all()
    assert any("平台名称" in (r.detail or "") and r.user == "admin" for r in rows)


def test_write_requires_admin():
    """写接口挂在 admin_only 依赖上：只读用户（viewer）应被 403 拦下。"""
    with pytest.raises(HTTPException) as err:
        admin_only({"sub": "viewer1", "role": "viewer"})
    assert err.value.status_code == 403
    # 管理员放行
    assert admin_only(ADMIN)["role"] == "admin"
