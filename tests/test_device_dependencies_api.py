# -*- coding: utf-8 -*-
"""device_dependencies 路由的环路防护测试。

注意：设备依赖模块已从 UI 取消（2026-09-10），告警收敛的依赖关系改为
**由拓扑连线自动推导**（见 tests/test_topology_dependency.py）。
本文件只保留该 API 自身的环路校验，以防将来重新启用或作为手工覆盖扩展点时回归。
"""
from app.models.device_dependency import DeviceDependency


async def test_cycle_is_rejected(db, device_factory):
    """A 依赖 B 后，B 依赖 A 必须被判定成环（否则两端互相抑制，故障时告警全消失）。"""
    from app.routers.device_dependencies import _would_create_cycle

    a = await device_factory(name="A", ip="10.0.0.10")
    b = await device_factory(name="B", ip="10.0.0.11")
    db.add(DeviceDependency(device_id=a.id, depends_on_device_id=b.id, enabled=True))
    await db.commit()

    assert await _would_create_cycle(db, b.id, a.id) is True
    # 反向再建一条 a→b 只是重复边，不构成环（重复由接口层的 409 拦下）
    assert await _would_create_cycle(db, a.id, b.id) is False
    c = await device_factory(name="C", ip="10.0.0.12")
    assert await _would_create_cycle(db, a.id, c.id) is False
