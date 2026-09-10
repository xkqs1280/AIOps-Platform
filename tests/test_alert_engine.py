"""告警规则引擎：判定逻辑与状态机。

覆盖点：
  - 静态阈值判定的全部运算符；
  - SNMP 取值/时间格式解析等纯函数；
  - 状态机「条件满足满 duration 才告警」「恢复自动 resolve」；
  - 动态基线模式：基线未就绪时不判定、偏离超阈值才告警、绝对下限生效。
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.models.alert import Alert
from app.services import alert_rule_engine as eng


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("condition,value,threshold,expected", [
    ("gt", 91, 90, True),
    ("gt", 90, 90, False),
    ("gte", 90, 90, True),
    ("lt", 79, 80, True),
    ("lt", 80, 80, False),
    ("lte", 80, 80, True),
    ("eq", 5, 5, True),
    ("ne", 5, 6, True),
    ("ne", 5, 5, False),
    ("unknown_op", 100, 1, False),
])
def test_check_condition(condition, value, threshold, expected):
    assert eng._check_condition(condition, value, threshold) is expected


def test_check_condition_none_value_never_violates():
    assert eng._check_condition("gt", None, 0) is False


@pytest.mark.parametrize("raw,expected", [
    ("up(1)", 1.0),
    ("down(2)", 2.0),
    ("12345", 12345.0),
    ("Timeticks: (456789) 1:16:07.89", 456789.0),
    (None, None),
    ("not-a-number", None),
])
def test_to_num(raw, expected):
    assert eng._to_num(raw) == expected


def test_fmt_ticks_units():
    # 入参是 TimeTicks（1/100 秒）
    assert eng._fmt_ticks(3000) == "30 秒"            # 30 秒
    assert eng._fmt_ticks(300000) == "50 分钟"         # 3000 秒
    assert eng._fmt_ticks(3600000) == "10.0 小时"      # 36000 秒
    assert eng._fmt_ticks(1000000000) == "115.7 天"    # 超 24 小时换算成天


def test_baseline_message_contains_evidence():
    """基线告警文案必须带上「当前值 / 基线 / 偏离倍数」，便于值班判断。"""
    rule = type("R", (), {
        "metric": "cpu_usage", "name": "CPU 偏离基线",
        "baseline_sigma": 3.0, "baseline_direction": "upper", "duration": 600,
    })()
    device = type("D", (), {"name": "SW1", "ip": "10.0.0.1"})()
    msg = eng._baseline_message(device, rule, 92.0, {"p50": 45.0}, 4.2)
    assert "92.0%" in msg and "45.0%" in msg and "+4.2" in msg and "3.0σ" in msg


# ---------------------------------------------------------------------------
# 状态机（静态阈值）
# ---------------------------------------------------------------------------

async def _settle(db, device, rule, value, baseline=None):
    """跑两轮评估。

    引擎状态机是「首轮记录违规起点，次轮起才按 duration 判定」——
    duration=0 也至少要两轮（真实环境两轮间隔 60s）。测试沿用同一语义。
    """
    await eng._eval_scalar(db, device, rule, value, baseline)
    await eng._eval_scalar(db, device, rule, value, baseline)
    await db.commit()


async def _active_alerts(db, device_id, rule_name):
    from sqlalchemy import select
    rows = (await db.execute(
        select(Alert).where(Alert.device_id == device_id, Alert.rule_name == rule_name)
    )).scalars().all()
    return rows


async def test_threshold_rule_waits_for_duration(db, device_factory, rule_factory):
    """未满 duration 不告警；满 duration 后创建 active 告警。"""
    device = await device_factory()
    rule = await rule_factory(duration=300)
    eng._state.clear()

    await eng._eval_scalar(db, device, rule, 95.0)
    await db.commit()
    assert await _active_alerts(db, device.id, rule.name) == []

    # 把违规起点回拨到 duration 之前 → 再评一次应触发
    eng._state[(device.id, rule.id)]["violation_start"] = (
        datetime.now(timezone.utc) - timedelta(seconds=400)
    )
    await eng._eval_scalar(db, device, rule, 95.0)
    await db.commit()

    rows = await _active_alerts(db, device.id, rule.name)
    assert len(rows) == 1
    assert rows[0].status == "active"
    assert rows[0].severity == "critical"
    assert "95" in rows[0].message


async def test_threshold_rule_recovers(db, device_factory, rule_factory):
    """条件恢复后自动 resolve，不残留僵尸告警。"""
    device = await device_factory()
    rule = await rule_factory(duration=0)
    eng._state.clear()

    await _settle(db, device, rule, 95.0)
    assert len(await _active_alerts(db, device.id, rule.name)) == 1

    await eng._eval_scalar(db, device, rule, 10.0)
    await db.commit()

    rows = await _active_alerts(db, device.id, rule.name)
    assert rows[0].status == "resolved"
    assert rows[0].resolved_at is not None


async def test_no_duplicate_alert_while_active(db, device_factory, rule_factory):
    """同一设备同一规则的活动告警只保留一条（去重）。"""
    device = await device_factory()
    rule = await rule_factory(duration=0)
    eng._state.clear()

    for _ in range(3):
        await eng._eval_scalar(db, device, rule, 95.0)
        await db.commit()

    assert len(await _active_alerts(db, device.id, rule.name)) == 1


# ---------------------------------------------------------------------------
# 状态机（动态基线）
# ---------------------------------------------------------------------------

async def test_baseline_mode_skips_without_baseline(db, device_factory, rule_factory):
    """基线未就绪（该时段样本不足）时不判定——既不告警也不恢复已有状态。"""
    device = await device_factory()
    rule = await rule_factory(name="CPU 基线", mode="baseline", threshold=0, duration=0)
    eng._state.clear()

    await eng._eval_scalar(db, device, rule, 99.0, baseline=None)
    await db.commit()
    assert await _active_alerts(db, device.id, rule.name) == []


async def test_baseline_mode_triggers_on_deviation(db, device_factory, rule_factory):
    """基线偏离超过 sigma 倍数 → 告警；文案体现基线信息。"""
    device = await device_factory()
    rule = await rule_factory(name="CPU 基线", mode="baseline", threshold=0, duration=0)
    eng._state.clear()

    baseline = {"p50": 40.0, "stddev": 5.0, "sample_count": 500}
    await _settle(db, device, rule, 70.0, baseline)  # z = +6

    rows = await _active_alerts(db, device.id, rule.name)
    assert len(rows) == 1
    assert "40.0%" in rows[0].message


async def test_baseline_mode_respects_absolute_floor(db, device_factory, rule_factory):
    """绝对下限（threshold）生效：夜间 1%→3% 这类小值大偏离不应告警。"""
    device = await device_factory()
    rule = await rule_factory(
        name="CPU 基线", mode="baseline", threshold=30.0, duration=0,
    )
    eng._state.clear()

    baseline = {"p50": 1.0, "stddev": 0.3, "sample_count": 500}
    await eng._eval_scalar(db, device, rule, 3.0, baseline)  # z 很大但 < 下限 30
    await db.commit()

    assert await _active_alerts(db, device.id, rule.name) == []


async def test_baseline_mode_ignores_zero_stddev(db, device_factory, rule_factory):
    """stddev=0（指标恒定）时不判定，避免除零与误报。"""
    device = await device_factory()
    rule = await rule_factory(name="CPU 基线", mode="baseline", threshold=0, duration=0)
    eng._state.clear()

    await eng._eval_scalar(db, device, rule, 99.0, {"p50": 50.0, "stddev": 0.0})
    await db.commit()
    assert await _active_alerts(db, device.id, rule.name) == []


# ---------------------------------------------------------------------------
# 端到端：evaluate_rules 一轮评估
# ---------------------------------------------------------------------------

async def test_evaluate_rules_creates_alert_without_dependency(db, device_factory, rule_factory):
    """无依赖配置时行为与改动前一致：超阈值直接产生活动告警。"""
    from sqlalchemy import select

    device = await device_factory(name="SW-独立", ip="10.0.0.1", cpu_usage=99.0)
    await rule_factory(name="CPU 过高", metric="cpu_usage", duration=0, threshold=90)

    await eng.evaluate_rules(db, [device])
    await eng.evaluate_rules(db, [device])

    rows = (await db.execute(select(Alert).where(Alert.device_id == device.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "active"


async def test_evaluate_rules_suppresses_downstream_alert(db, device_factory, rule_factory):
    """上游离线时，下游的连带告警在一轮评估后落库为 suppressed 并标注来源设备。

    依赖不再手工配置：由拓扑连线推导（路由器 > 交换机）。
    """
    from sqlalchemy import select

    from app.models.topology_link import TopologyLink

    core = await device_factory(name="核心路由器", ip="10.0.0.1", status="offline", device_type="router")
    access = await device_factory(name="接入交换机", ip="10.0.0.2", cpu_usage=99.0, device_type="switch")
    lo, hi = (core, access) if core.id < access.id else (access, core)
    db.add(TopologyLink(source_device_id=lo.id, target_device_id=hi.id, link_type="ethernet"))
    await db.commit()
    await rule_factory(name="CPU 过高", metric="cpu_usage", duration=0, threshold=90)

    await eng.evaluate_rules(db, [core, access])
    await eng.evaluate_rules(db, [core, access])

    rows = (await db.execute(select(Alert).where(Alert.device_id == access.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "suppressed"
    assert rows[0].suppressed_by_device_id == core.id
    assert "核心路由器" in rows[0].suppress_reason


async def test_evaluate_rules_releases_suppression_after_upstream_recovers(db, device_factory, rule_factory):
    """上游恢复后，被抑制的告警应被解除（置 resolved），不会永久挂着。"""
    from sqlalchemy import select

    from app.models.topology_link import TopologyLink

    core = await device_factory(name="核心路由器", ip="10.0.0.1", status="offline", device_type="router")
    access = await device_factory(name="接入交换机", ip="10.0.0.2", cpu_usage=99.0, device_type="switch")
    lo, hi = (core, access) if core.id < access.id else (access, core)
    db.add(TopologyLink(source_device_id=lo.id, target_device_id=hi.id, link_type="ethernet"))
    await db.commit()
    await rule_factory(name="CPU 过高", metric="cpu_usage", duration=0, threshold=90)

    await eng.evaluate_rules(db, [core, access])
    await eng.evaluate_rules(db, [core, access])
    suppressed = (await db.execute(select(Alert))).scalars().all()
    assert [a.status for a in suppressed] == ["suppressed"]

    # 上游恢复在线后重跑一轮
    core.status = "online"
    await db.commit()
    await eng.evaluate_rules(db, [core, access])

    rows = (await db.execute(select(Alert))).scalars().all()
    by_status = {a.status for a in rows}
    # 旧的被抑制告警已解除；因条件仍满足，新一轮会重新产生一条活动告警
    assert "suppressed" not in by_status
    assert "active" in by_status
