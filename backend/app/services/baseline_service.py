"""动态基线引擎：计算设备指标统计基线并检测偏差"""
from datetime import datetime

import numpy as np
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.p2_baseline import MetricBaseline
from app.models.device import Device
from app.database import get_session  # noqa: F401 — re-exported for callers


# ---------------------------------------------------------------------------
# Metric simulation profiles
# ---------------------------------------------------------------------------

_METRIC_PROFILES = {
    "cpu_usage":       {"base_p50": 50, "amplitude": 8,  "stddev": 6, "vmin": 0,  "vmax": 100},
    "memory_usage":    {"base_p50": 65, "amplitude": 2,  "stddev": 3, "vmin": 0,  "vmax": 100},
    "temperature":     {"base_p50": 50, "amplitude": 5,  "stddev": 4, "vmin": 20, "vmax": 90},
    "bandwidth_usage": {"base_p50": 40, "amplitude": 10, "stddev": 8, "vmin": 0,  "vmax": 100},
}

_METRIC_SEEDS = {
    "cpu_usage": 101,
    "memory_usage": 202,
    "temperature": 303,
    "bandwidth_usage": 404,
}


def _diurnal_factor(hour: int) -> float:
    """日间因子：6-18 时正弦曲线，正午达到峰值 1.0，其余时段为 0。"""
    return max(0.0, float(np.sin(np.pi * (hour - 6) / 12)))


def _generate_hourly_stats(metric_name: str, hour: int, days: int) -> dict:
    """为单个指标 + 小时生成模拟统计基线数据。"""
    profile = _METRIC_PROFILES[metric_name]
    p50_center = profile["base_p50"] + profile["amplitude"] * _diurnal_factor(hour)
    std = profile["stddev"]

    seed = _METRIC_SEEDS[metric_name] * 100 + hour
    rng = np.random.default_rng(seed=seed)
    n_samples = max(days * 6, 30)
    samples = rng.normal(loc=p50_center, scale=std, size=n_samples)
    samples = np.clip(samples, profile["vmin"], profile["vmax"])

    return {
        "p5": float(np.percentile(samples, 5)),
        "p25": float(np.percentile(samples, 25)),
        "p50": float(np.percentile(samples, 50)),
        "p75": float(np.percentile(samples, 75)),
        "p95": float(np.percentile(samples, 95)),
        "stddev": float(np.std(samples)),
        "sample_count": n_samples,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def calculate_baselines(
    session: AsyncSession,
    device_id: int | None = None,
    days: int = 30,
) -> int:
    """计算设备指标的统计基线并执行 upsert（基于 device_id + metric_name + hour_of_day）。

    由于尚未接入真实 TSDB，使用模拟数据生成基线。

    Returns:
        更新的基线记录总数。
    """
    query = select(Device.id)
    if device_id is not None:
        query = query.where(Device.id == device_id)
    result = await session.execute(query)
    device_ids = [row[0] for row in result.all()]

    if not device_ids:
        return 0

    now = datetime.utcnow()
    total = 0

    for dev_id in device_ids:
        for metric_name, profile in _METRIC_PROFILES.items():
            for hour in range(24):
                stats = _generate_hourly_stats(metric_name, hour, days)

                stmt = pg_insert(MetricBaseline).values(
                    device_id=dev_id,
                    metric_name=metric_name,
                    hour_of_day=hour,
                    p5=stats["p5"],
                    p25=stats["p25"],
                    p50=stats["p50"],
                    p75=stats["p75"],
                    p95=stats["p95"],
                    stddev=stats["stddev"],
                    sample_count=stats["sample_count"],
                    updated_at=now,
                )
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_baseline",
                    set_={
                        "p5": stmt.excluded.p5,
                        "p25": stmt.excluded.p25,
                        "p50": stmt.excluded.p50,
                        "p75": stmt.excluded.p75,
                        "p95": stmt.excluded.p95,
                        "stddev": stmt.excluded.stddev,
                        "sample_count": stmt.excluded.sample_count,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
                await session.execute(stmt)
                total += 1

    await session.commit()
    return total


async def detect_baseline_deviation(
    session: AsyncSession,
    device_id: int,
    metric_name: str,
    current_value: float,
    current_hour: int,
) -> dict | None:
    """检测当前值是否偏离基线。

    z-score = (current_value - p50) / stddev

    - |z| > 4 → "critical" (P0)
    - |z| > 3 → "major"    (P1)
    - |z| > 2 → "minor"    (P2)
    - 否则     → severity 为 None

    Returns:
        {"severity", "z_score", "baseline_p50", "current_value"},
        若无基线或 stddev 为 0 则返回 None。
    """
    result = await session.execute(
        select(MetricBaseline).where(
            MetricBaseline.device_id == device_id,
            MetricBaseline.metric_name == metric_name,
            MetricBaseline.hour_of_day == current_hour,
        )
    )
    baseline = result.scalar_one_or_none()

    if baseline is None:
        return None

    if baseline.stddev is None or baseline.stddev == 0:
        return None

    z_score = (current_value - baseline.p50) / baseline.stddev
    abs_z = abs(z_score)

    if abs_z > 4:
        severity = "critical"
    elif abs_z > 3:
        severity = "major"
    elif abs_z > 2:
        severity = "minor"
    else:
        severity = None

    return {
        "severity": severity,
        "z_score": round(z_score, 2),
        "baseline_p50": baseline.p50,
        "current_value": current_value,
    }


async def get_device_baselines(session: AsyncSession, device_id: int) -> dict:
    """获取设备的所有基线记录，按 metric_name 分组。

    Returns:
        {metric_name: [{hour_of_day, p5, p25, p50, p75, p95, stddev, ...}, ...]}
    """
    result = await session.execute(
        select(MetricBaseline)
        .where(MetricBaseline.device_id == device_id)
        .order_by(MetricBaseline.metric_name, MetricBaseline.hour_of_day)
    )
    records = result.scalars().all()

    grouped: dict[str, list[dict]] = {}
    for record in records:
        grouped.setdefault(record.metric_name, []).append({
            "hour_of_day": record.hour_of_day,
            "p5": record.p5,
            "p25": record.p25,
            "p50": record.p50,
            "p75": record.p75,
            "p95": record.p95,
            "stddev": record.stddev,
            "sample_count": record.sample_count,
            "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        })

    return grouped
