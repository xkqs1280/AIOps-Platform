"""动态基线：偏离判定与统计口径。

判定函数是纯函数，因此可以穷举边界；重点是「宁可漏报也不误报」的几条保护：
无基线 / stddev 为 0 / 低于绝对下限，都不应产生告警。
"""
import pytest

from app.services.baseline_service import (
    METRIC_ALIASES,
    MIN_SAMPLES,
    evaluate_baseline_violation,
)


def test_metric_aliases_cover_rule_metrics():
    """metric_records 的 metric_type 与告警规则的 metric 名必须映射得上，
    否则基线永远算不出来（历史上这里就是断的）。"""
    assert METRIC_ALIASES["cpu"] == "cpu_usage"
    assert METRIC_ALIASES["memory"] == "memory_usage"
    assert METRIC_ALIASES["temperature"] == "temperature"


def test_min_samples_is_sane():
    assert MIN_SAMPLES >= 10


@pytest.mark.parametrize("value,p50,stddev,sigma,direction,expected", [
    # 正偏离
    (70.0, 40.0, 5.0, 3.0, "upper", True),    # z=+6 > 3
    (50.0, 40.0, 5.0, 3.0, "upper", False),   # z=+2 < 3
    # 负偏离
    (20.0, 40.0, 5.0, 3.0, "lower", True),    # z=-4 < -3
    (35.0, 40.0, 5.0, 3.0, "lower", False),
    # 双向
    (20.0, 40.0, 5.0, 3.0, "both", True),
    (70.0, 40.0, 5.0, 3.0, "both", True),
    (45.0, 40.0, 5.0, 3.0, "both", False),
    # 只看上行时，下行偏离不告警
    (20.0, 40.0, 5.0, 3.0, "upper", False),
])
def test_direction_and_sigma(value, p50, stddev, sigma, direction, expected):
    violating, z = evaluate_baseline_violation(value, p50, stddev, sigma, direction)
    assert violating is expected
    assert z == pytest.approx((value - p50) / stddev, abs=0.01)


@pytest.mark.parametrize("value,p50,stddev", [
    (70.0, None, 5.0),    # 无基线
    (None, 40.0, 5.0),    # 无当前值
    (70.0, 40.0, None),   # 无标准差
    (70.0, 40.0, 0.0),    # 标准差为 0（指标恒定）
    (70.0, 40.0, -1.0),   # 异常标准差
])
def test_never_violates_without_usable_baseline(value, p50, stddev):
    violating, _ = evaluate_baseline_violation(value, p50, stddev)
    assert violating is False


def test_absolute_floor_blocks_meaningless_deviation():
    """夜间 CPU 从 1% 涨到 3%，z 值很大但业务上无意义 → 绝对下限拦住。"""
    violating, z = evaluate_baseline_violation(
        3.0, p50=1.0, stddev=0.3, sigma=3.0, direction="upper", min_abs=30.0
    )
    assert violating is False
    assert z is not None  # 仍然回报 z，便于观测


def test_absolute_floor_does_not_block_real_spike():
    violating, _ = evaluate_baseline_violation(
        92.0, p50=40.0, stddev=5.0, sigma=3.0, direction="upper", min_abs=30.0
    )
    assert violating is True


def test_custom_sigma_is_respected():
    # z = +2.5：sigma=2 时告警，sigma=3 时不告警
    assert evaluate_baseline_violation(52.5, 40.0, 5.0, sigma=2.0)[0] is True
    assert evaluate_baseline_violation(52.5, 40.0, 5.0, sigma=3.0)[0] is False


async def test_load_baseline_map_returns_keyed_by_device_and_metric(db, device_factory):
    """批量加载必须按 (device_id, metric) 建索引，供引擎每轮一次取用。"""
    from app.models.p2_baseline import MetricBaseline
    from app.services.baseline_service import load_baseline_map

    device = await device_factory()
    for hour in (9, 10, 11):
        db.add(MetricBaseline(
            device_id=device.id, metric_name="cpu_usage", hour_of_day=hour,
            p50=40.0 + hour, stddev=5.0, sample_count=500,
        ))
    await db.commit()

    mapping = await load_baseline_map(db, 10, [device.id])
    assert (device.id, "cpu_usage") in mapping
    assert mapping[(device.id, "cpu_usage")]["p50"] == 50.0

    # 其它时段不应混入
    assert len([k for k in mapping if k[0] == device.id]) == 1
