# -*- coding: utf-8 -*-
"""拓扑依赖推导测试。

依赖关系不再手工配置：完全由拓扑连线推导（类型层级 + 同层级连接数）。
核心断言点是"不能把方向判反"——方向错了会把真实告警压掉（漏报）。
"""
from app.models.topology_link import TopologyLink
from app.services.topology_dependency_service import (
    DEGREE_GAP,
    derive_dependencies,
    describe_dependencies,
    rank_of,
)


async def _link(db, a, b, link_type: str = "ethernet"):
    """建立一条连线（落库时按 id 规范化，与调用顺序无关）。"""
    lo, hi = (a, b) if a.id < b.id else (b, a)
    db.add(TopologyLink(source_device_id=lo.id, target_device_id=hi.id, link_type=link_type))
    await db.commit()


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------

async def test_empty_topology_has_no_dependency(db):
    derived = await derive_dependencies(db)
    assert derived["deps"] == {}
    assert derived["details"] == []
    assert derived["peer_pairs"] == []


async def test_type_hierarchy_decides_direction(db, device_factory):
    """防火墙 > 服务器：服务器是下游。"""
    fw = await device_factory(name="FW", ip="10.0.0.1", device_type="firewall")
    srv = await device_factory(name="SRV", ip="10.0.0.9", device_type="server")
    await _link(db, fw, srv)

    derived = await derive_dependencies(db)
    assert derived["deps"] == {srv.id: [fw.id]}
    detail = derived["details"][0]
    assert detail["basis"] == "type"
    assert detail["upstream_name"] == "FW"
    assert detail["downstream_name"] == "SRV"


async def test_router_above_switch_above_server(db, device_factory):
    """链式：路由器 - 交换机 - 服务器，方向逐级向下。"""
    rtr = await device_factory(name="RTR", ip="10.0.0.1", device_type="router")
    sw = await device_factory(name="SW", ip="10.0.0.2", device_type="switch")
    srv = await device_factory(name="SRV", ip="10.0.0.9", device_type="server")
    await _link(db, rtr, sw)
    await _link(db, sw, srv)

    deps = (await derive_dependencies(db))["deps"]
    assert deps == {sw.id: [rtr.id], srv.id: [sw.id]}


async def test_direction_is_independent_of_link_order(db, device_factory):
    """连线落库顺序不影响方向判定。"""
    srv = await device_factory(name="SRV", ip="10.0.0.9", device_type="server")
    fw = await device_factory(name="FW", ip="10.0.0.1", device_type="firewall")
    # 先建 server，后建 firewall → source/target 会被规范化，方向仍由层级决定
    await _link(db, srv, fw)

    deps = (await derive_dependencies(db))["deps"]
    assert deps == {srv.id: [fw.id]}


async def test_unknown_type_falls_back_to_switch_rank(db, device_factory):
    """未填类型按交换机层级处理。"""
    unknown = await device_factory(name="X", ip="10.0.0.2", device_type=None)
    srv = await device_factory(name="SRV", ip="10.0.0.9", device_type="server")
    await _link(db, unknown, srv)

    assert rank_of(None) == rank_of("switch")
    deps = (await derive_dependencies(db))["deps"]
    assert deps == {srv.id: [unknown.id]}


# ---------------------------------------------------------------------------
# 同层级：连接数启发式（保守）
# ---------------------------------------------------------------------------

async def test_same_type_uses_degree_gap(db, device_factory):
    """同为交换机时，连接数相差达到阈值 → 少的一方为下游。"""
    core = await device_factory(name="CORE", ip="10.0.0.2", device_type="switch")
    acc1 = await device_factory(name="ACC1", ip="10.0.0.11", device_type="switch")
    acc2 = await device_factory(name="ACC2", ip="10.0.0.12", device_type="switch")
    acc3 = await device_factory(name="ACC3", ip="10.0.0.13", device_type="switch")
    await _link(db, core, acc1)
    await _link(db, core, acc2)
    await _link(db, core, acc3)

    derived = await derive_dependencies(db)
    assert derived["deps"][acc1.id] == [core.id]
    assert derived["deps"][acc2.id] == [core.id]
    assert derived["deps"][acc3.id] == [core.id]
    assert all(d["basis"] == "degree" for d in derived["details"])


async def test_same_type_close_degree_is_peer(db, device_factory):
    """两台交换机单线直连（度数相同）→ 对等互联，不建立依赖（防止方向判反）。"""
    a = await device_factory(name="SW-A", ip="10.0.0.2", device_type="switch")
    b = await device_factory(name="SW-B", ip="10.0.0.3", device_type="switch")
    await _link(db, a, b)

    derived = await derive_dependencies(db)
    assert derived["deps"] == {}
    assert len(derived["peer_pairs"]) == 1
    assert DEGREE_GAP == 2


async def test_ring_of_same_type_yields_no_mutual_dependency(db, device_factory):
    """三台同型号交换机环网互连：度数相同 → 全部对等，不会互相抑制。"""
    a = await device_factory(name="A", ip="10.0.0.1", device_type="switch")
    b = await device_factory(name="B", ip="10.0.0.2", device_type="switch")
    c = await device_factory(name="C", ip="10.0.0.3", device_type="switch")
    await _link(db, a, b)
    await _link(db, b, c)
    await _link(db, a, c)

    derived = await derive_dependencies(db)
    assert derived["deps"] == {}
    assert len(derived["peer_pairs"]) == 3


async def test_duplicate_pair_creates_single_dependency(db, device_factory):
    """同一对设备重复连线只产生一条依赖。"""
    sw = await device_factory(name="SW", ip="10.0.0.2", device_type="switch")
    srv = await device_factory(name="SRV", ip="10.0.0.9", device_type="server")
    await _link(db, sw, srv)

    deps = (await derive_dependencies(db))["deps"]
    assert deps[srv.id] == [sw.id]


# ---------------------------------------------------------------------------
# 与告警抑制的衔接
# ---------------------------------------------------------------------------

async def test_build_dependency_context_comes_from_topology(db, device_factory):
    """告警抑制上下文改由拓扑推导：上游离线即进入 upstream_down。"""
    from app.services.alert_suppressor import build_dependency_context

    fw = await device_factory(name="FW", ip="10.0.0.1", device_type="firewall", status="offline")
    srv = await device_factory(name="SRV", ip="10.0.0.9", device_type="server", status="online")
    await _link(db, fw, srv)

    ctx = await build_dependency_context(db)
    assert ctx["deps"] == {srv.id: [fw.id]}
    assert ctx["downstreams"] == {fw.id: [srv.id]}
    assert fw.id in ctx["upstream_down"]


async def test_build_dependency_context_empty_without_links(db, device_factory):
    """没有任何连线时依赖上下文为空（对存量部署零影响）。"""
    from app.services.alert_suppressor import build_dependency_context

    await device_factory(name="FW", ip="10.0.0.1", device_type="firewall", status="offline")
    ctx = await build_dependency_context(db)
    assert ctx == {"deps": {}, "downstreams": {}, "upstream_down": {}}


async def test_describe_dependencies_contains_status(db, device_factory):
    """API 输出带上上下游当前状态，便于前端高亮。"""
    fw = await device_factory(name="FW", ip="10.0.0.1", device_type="firewall", status="offline")
    srv = await device_factory(name="SRV", ip="10.0.0.9", device_type="server", status="online")
    await _link(db, fw, srv)

    data = await describe_dependencies(db)
    assert data["summary"]["dependency_count"] == 1
    item = data["dependencies"][0]
    assert item["upstream_name"] == "FW"
    assert item["upstream_status"] == "offline"
    assert item["downstream_status"] == "online"
    assert item["reason"]
