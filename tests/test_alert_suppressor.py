"""告警收敛：拓扑依赖抑制 / 抖动抑制 / 风暴聚合。

这三条是「少打扰」的核心保障，任何一条失效都会让告警重新刷屏，
所以每条都单独验证正例与反例（尤其是 critical 不被风暴吞掉）。
"""
from sqlalchemy import select

from app.models.alert import Alert
from app.models.topology_link import TopologyLink
from app.services import alert_suppressor as sup


async def _link(db, a, b):
    """建立拓扑连线——依赖关系现在完全由连线推导，不再手工配置。"""
    lo, hi = (a, b) if a.id < b.id else (b, a)
    db.add(TopologyLink(source_device_id=lo.id, target_device_id=hi.id, link_type="ethernet"))
    await db.commit()


# ---------------------------------------------------------------------------
# 拓扑依赖抑制
# ---------------------------------------------------------------------------

async def test_no_dependency_config_never_suppresses(db, device_factory):
    """拓扑中没有连线时推导不出任何依赖——抑制逻辑完全不生效，存量部署零影响。"""
    device = await device_factory()
    ctx = await sup.build_dependency_context(db)
    assert ctx["deps"] == {}

    decision = sup.evaluate_suppression(device, "CPU 使用率过高", "critical", ctx)
    assert decision["suppressed"] is False
    assert decision["notify"] is True


async def test_downstream_suppressed_when_upstream_offline(db, device_factory):
    """上游离线时，下游新告警被抑制并记录来源设备（依赖来自拓扑连线）。"""
    core = await device_factory(name="核心路由器", ip="10.0.0.1", status="offline", device_type="router")
    access = await device_factory(name="接入交换机", ip="10.0.0.2", status="online", device_type="switch")
    await _link(db, core, access)

    ctx = await sup.build_dependency_context(db)
    decision = sup.evaluate_suppression(access, "接口状态异常", "warning", ctx)

    assert decision["suppressed"] is True
    assert decision["by_device_id"] == core.id
    assert decision["notify"] is False
    assert "核心路由器" in decision["reason"]


async def test_downstream_not_suppressed_when_upstream_healthy(db, device_factory):
    core = await device_factory(name="核心路由器", ip="10.0.0.1", status="online", device_type="router")
    access = await device_factory(name="接入交换机", ip="10.0.0.2", status="online", device_type="switch")
    await _link(db, core, access)

    ctx = await sup.build_dependency_context(db)
    decision = sup.evaluate_suppression(access, "接口状态异常", "warning", ctx)
    assert decision["suppressed"] is False


async def test_same_level_peers_never_suppress(db, device_factory):
    """同层级且连接数接近的设备判为对等互联 → 不建立依赖、不会互相抑制。"""
    core = await device_factory(name="核心交换机", ip="10.0.0.1", status="offline", device_type="switch")
    access = await device_factory(name="接入交换机", ip="10.0.0.2", status="online", device_type="switch")
    await _link(db, core, access)

    ctx = await sup.build_dependency_context(db)
    assert ctx["deps"] == {}
    assert sup.evaluate_suppression(access, "接口状态异常", "warning", ctx)["suppressed"] is False


async def test_upstream_down_detected_via_active_alert(db, device_factory):
    """上游设备状态还是 online，但存在活动「设备不可达」告警 → 同样算上游异常。"""
    core = await device_factory(name="核心路由器", ip="10.0.0.1", status="online", device_type="router")
    access = await device_factory(name="接入交换机", ip="10.0.0.2", status="online", device_type="switch")
    await _link(db, core, access)
    db.add(Alert(
        device_id=core.id, rule_name="设备不可达", severity="critical",
        message="不可达", status="active",
    ))
    await db.commit()

    ctx = await sup.build_dependency_context(db)
    assert sup.evaluate_suppression(access, "接口状态异常", "warning", ctx)["suppressed"] is True


# ---------------------------------------------------------------------------
# 抖动抑制
# ---------------------------------------------------------------------------

def test_flap_suppresses_notification_but_keeps_alert():
    """反复恢复达阈值后，仅压掉通知，告警本身仍要可见。"""
    device = type("D", (), {"id": 1, "name": "SW1", "ip": "10.0.0.1"})()
    for _ in range(sup.FLAP_THRESHOLD):
        sup.record_resolution(1, "接口状态异常")

    decision = sup.evaluate_suppression(device, "接口状态异常", "warning", None)
    assert decision["flap_count"] >= sup.FLAP_THRESHOLD
    assert decision["notify"] is False
    assert decision["suppressed"] is False  # 告警仍然要创建


def test_below_flap_threshold_notifies():
    device = type("D", (), {"id": 2, "name": "SW2", "ip": "10.0.0.2"})()
    for _ in range(sup.FLAP_THRESHOLD - 1):
        sup.record_resolution(2, "接口状态异常")
    decision = sup.evaluate_suppression(device, "接口状态异常", "warning", None)
    assert decision["notify"] is True
    assert decision["flap_count"] < sup.FLAP_THRESHOLD


# ---------------------------------------------------------------------------
# 风暴聚合
# ---------------------------------------------------------------------------

def test_storm_suppresses_non_critical():
    device = type("D", (), {"id": 3, "name": "SW3", "ip": "10.0.0.3"})()
    for _ in range(sup.STORM_THRESHOLD):
        sup.record_alert_created(3)

    decision = sup.evaluate_suppression(device, "温度过高", "warning", None)
    assert decision["suppressed"] is True
    assert "告警风暴" in decision["reason"]


def test_storm_never_swallows_critical():
    """critical 永远放行——风暴折叠不能把真正的严重故障吞掉。"""
    device = type("D", (), {"id": 4, "name": "SW4", "ip": "10.0.0.4"})()
    for _ in range(sup.STORM_THRESHOLD * 2):
        sup.record_alert_created(4)

    decision = sup.evaluate_suppression(device, "设备离线", "critical", None)
    assert decision["suppressed"] is False


async def test_storm_summary_created_and_updated(db, device_factory):
    device = await device_factory(name="SW5", ip="10.0.0.5")

    await sup.ensure_storm_summary(db, device, 16)
    await db.commit()
    rows = (await db.execute(
        select(Alert).where(Alert.rule_name == sup.STORM_SUMMARY_RULE)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].aggregated_count == 16
    assert rows[0].severity == "major"

    # 再次触发只更新同一条，不重复刷
    await sup.ensure_storm_summary(db, device, 25)
    await db.commit()
    rows = (await db.execute(
        select(Alert).where(Alert.rule_name == sup.STORM_SUMMARY_RULE)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].aggregated_count == 25

    await sup.resolve_storm_summary(db, device.id)
    await db.commit()
    row = (await db.execute(
        select(Alert).where(Alert.rule_name == sup.STORM_SUMMARY_RULE)
    )).scalars().first()
    assert row.status == "resolved"


# ---------------------------------------------------------------------------
# 抑制告警的解除
# ---------------------------------------------------------------------------

async def test_resolve_suppressed_alerts_only_touches_dependency_ones(db, device_factory):
    device = await device_factory(name="SW6", ip="10.0.0.6")
    db.add(Alert(
        device_id=device.id, rule_name="接口状态异常", severity="warning",
        message="被上游抑制", status="suppressed", suppressed_by_device_id=99,
    ))
    db.add(Alert(
        device_id=device.id, rule_name="温度过高", severity="warning",
        message="风暴折叠", status="suppressed", suppressed_by_device_id=None,
    ))
    await db.commit()

    count = await sup.resolve_suppressed_alerts(db, device.id)
    await db.commit()
    assert count == 1

    rows = (await db.execute(select(Alert))).scalars().all()
    by_rule = {r.rule_name: r for r in rows}
    assert by_rule["接口状态异常"].status == "resolved"
    # 因风暴折叠的告警不属于依赖抑制，不应被上游恢复误伤
    assert by_rule["温度过高"].status == "suppressed"


# ---------------------------------------------------------------------------
# 环路防护
# ---------------------------------------------------------------------------
# 「设备依赖」模块已取消（依赖改由拓扑连线自动推导）：原 test_cycle_is_rejected
# 已移至 tests/test_device_dependencies_api.py，仅保留该 API 自身的环路校验。
