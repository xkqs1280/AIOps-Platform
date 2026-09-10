"""动态基线引擎：从真实时序数据计算设备指标的分时段统计基线，并提供偏离判定。

背景（重要）：
  本模块早期版本用 numpy 随机数"模拟"生成基线（`_generate_hourly_stats`），
  从未读取真实数据，因此基线虽然能算、却是假数据，无法用于告警判定。
  现改为直接从 `metric_records`（指标采集器每 60s 落库的真实采样）聚合计算。

模型：
  - 按 设备 × 指标 × 小时内时段（0-23）聚合，得到该时段的 p5/p25/p50/p75/p95/stddev/样本数；
  - 时段一律使用 **东八区本地小时**：业务负载按本地作息走，用 UTC 小时会让基线整体错位 8 小时，
    直接导致误报爆炸；
  - 样本数低于 MIN_SAMPLES 的时段不产出基线（避免用几天的噪声当"正常值"）。

判定：
  z = (当前值 - p50) / stddev，按 baseline_direction 决定关注正偏离/负偏离/双向；
  规则表 threshold 字段在基线模式下作为**绝对下限**（防止 1% → 3% 这类无意义的小值大偏离）。
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import Integer, cast, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import engine
from app.models.device import Device
from app.models.metric_record import MetricRecord
from app.models.p2_baseline import MetricBaseline

logger = logging.getLogger(__name__)

# 平台面向国内运维场景，基线时段固定按东八区计算（与前端展示、值班作息一致）
LOCAL_TZ = "Asia/Shanghai"
LOCAL_TZ_OFFSET = timedelta(hours=8)

# 参与基线计算的指标：metric_records.metric_type -> 告警规则 metric 名
METRIC_ALIASES = {
    "cpu": "cpu_usage",
    "memory": "memory_usage",
    "temperature": "temperature",
}

# 每个时段的样本下限：采集间隔 60s 时一小时最多 60 条，30 天同一时段约 1800 条。
# 低于 20 条说明采集刚起步或设备长期离线，此时基线不可信，不产出。
MIN_SAMPLES = 20

# 基线场景允许的指标（与告警引擎 SUPPORTED_METRICS 的交集）
BASELINE_METRICS = tuple(METRIC_ALIASES.values())

# 默认回看天数（metric_records 保留期为 30 天，留 2 天余量）
DEFAULT_LOOKBACK_DAYS = 28

_UPSERT_CHUNK = 1000


# ---------------------------------------------------------------------------
# 计算
# ---------------------------------------------------------------------------

def _is_postgres() -> bool:
    return engine.dialect.name == "postgresql"


def _local_hour_expr():
    """东八区小时表达式（仅 PostgreSQL）。"""
    local_ts = func.timezone(LOCAL_TZ, MetricRecord.recorded_at)
    return cast(func.extract("hour", local_ts), Integer)


async def _aggregate_stats(
    session: AsyncSession, since: datetime, device_id: int | None
) -> list[dict]:
    """按 设备 × 指标 × 本地小时 聚合统计量（PostgreSQL 走 SQL 聚合，避免拉全量数据到内存）。"""
    if not _is_postgres():
        raise RuntimeError("基线计算依赖 PostgreSQL 的 percentile_cont，当前数据库不支持")

    hour_expr = _local_hour_expr()
    stmt = (
        select(
            MetricRecord.device_id.label("device_id"),
            MetricRecord.metric_type.label("metric_type"),
            hour_expr.label("hod"),
            func.percentile_cont(0.05).within_group(MetricRecord.value.asc()).label("p5"),
            func.percentile_cont(0.25).within_group(MetricRecord.value.asc()).label("p25"),
            func.percentile_cont(0.50).within_group(MetricRecord.value.asc()).label("p50"),
            func.percentile_cont(0.75).within_group(MetricRecord.value.asc()).label("p75"),
            func.percentile_cont(0.95).within_group(MetricRecord.value.asc()).label("p95"),
            func.stddev_samp(MetricRecord.value).label("stddev"),
            func.count().label("n"),
        )
        .where(MetricRecord.recorded_at >= since, MetricRecord.value.isnot(None))
        .group_by(MetricRecord.device_id, MetricRecord.metric_type, hour_expr)
    )
    if device_id is not None:
        stmt = stmt.where(MetricRecord.device_id == device_id)

    rows = (await session.execute(stmt)).all()
    out: list[dict] = []
    for r in rows:
        metric_name = METRIC_ALIASES.get((r.metric_type or "").lower())
        if metric_name is None:
            continue
        if r.hod is None or r.n is None or int(r.n) < MIN_SAMPLES:
            continue
        out.append({
            "device_id": r.device_id,
            "metric_name": metric_name,
            "hour_of_day": int(r.hod),
            "p5": float(r.p5) if r.p5 is not None else None,
            "p25": float(r.p25) if r.p25 is not None else None,
            "p50": float(r.p50) if r.p50 is not None else None,
            "p75": float(r.p75) if r.p75 is not None else None,
            "p95": float(r.p95) if r.p95 is not None else None,
            "stddev": float(r.stddev) if r.stddev is not None else None,
            "sample_count": int(r.n),
        })
    return out


async def calculate_baselines(
    session: AsyncSession,
    device_id: int | None = None,
    days: int = DEFAULT_LOOKBACK_DAYS,
) -> int:
    """从真实 metric_records 计算分时段基线并 upsert，返回写入的基线行数。

    幂等：以 (device_id, metric_name, hour_of_day) 唯一键 upsert，可重复执行。
    某时段样本不足时不写入（也不会删除已有基线，避免设备临时离线把基线抹掉）。
    """
    if device_id is not None:
        device_ids = [device_id]
    else:
        device_ids = [r[0] for r in (await session.execute(select(Device.id))).all()]
    if not device_ids:
        return 0

    since = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    stats = await _aggregate_stats(session, since, device_id)
    if not stats:
        logger.info("基线计算：近 %s 天无足够采样数据（每时段需 >= %s 条）", days, MIN_SAMPLES)
        return 0

    now = datetime.now(timezone.utc)
    for row in stats:
        row["updated_at"] = now

    if _is_postgres():
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        total = 0
        for i in range(0, len(stats), _UPSERT_CHUNK):
            chunk = stats[i:i + _UPSERT_CHUNK]
            stmt = pg_insert(MetricBaseline).values(chunk)
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
            total += len(chunk)
        await session.commit()
        logger.info("基线计算完成：写入 %s 条（%s 台设备）", total, len(device_ids))
        return total

    # 非 PostgreSQL（仅测试环境）：逐行合并，语义与 upsert 一致
    total = 0
    for row in stats:
        existing = (await session.execute(
            select(MetricBaseline).where(
                MetricBaseline.device_id == row["device_id"],
                MetricBaseline.metric_name == row["metric_name"],
                MetricBaseline.hour_of_day == row["hour_of_day"],
            )
        )).scalar_one_or_none()
        if existing is None:
            existing = MetricBaseline(**row)
            session.add(existing)
        else:
            for k, v in row.items():
                setattr(existing, k, v)
        total += 1
    await session.commit()
    return total


async def purge_baselines_for_device(session: AsyncSession, device_id: int) -> int:
    """设备删除后清理其基线（避免孤儿数据堆积）。"""
    res = await session.execute(
        delete(MetricBaseline).where(MetricBaseline.device_id == device_id)
    )
    await session.commit()
    return res.rowcount or 0


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------

def evaluate_baseline_violation(
    value: float | None,
    p50: float | None,
    stddev: float | None,
    sigma: float = 3.0,
    direction: str = "upper",
    min_abs: float = 0.0,
) -> tuple[bool, float | None]:
    """判断当前值是否偏离基线。返回 (是否违规, z-score)。

    - 基线与标准差缺失或 stddev<=0 → 不判定（宁可漏报也不误报）；
    - min_abs（规则 threshold 字段）为绝对下限：低于该值的小数值即使 z 很大也不告警，
      否则夜间 CPU 从 1% 涨到 3% 会被判成"异常偏离"；
    - direction: upper=仅正偏离 / lower=仅负偏离 / both=双向。
    """
    if value is None or p50 is None or stddev is None or stddev <= 0:
        return False, None
    if min_abs and abs(value) < min_abs:
        return False, round((value - p50) / stddev, 2)

    z = (value - p50) / stddev
    if direction == "lower":
        violating = z < -sigma
    elif direction == "both":
        violating = abs(z) > sigma
    else:  # upper（默认）
        violating = z > sigma
    return violating, round(z, 2)


async def load_baseline_map(
    session: AsyncSession,
    hour: int,
    device_ids: list[int] | None = None,
) -> dict[tuple[int, str], dict]:
    """批量加载指定时段的基线，返回 {(device_id, metric_name): {p50, stddev, sample_count}}。

    告警引擎每轮评估是"一次评估 90 台设备 × N 条规则"，逐条查库会产生数百次查询，
    故在评估轮开始时一次性把当小时的全部基线读进内存。
    """
    stmt = select(MetricBaseline).where(MetricBaseline.hour_of_day == int(hour))
    if device_ids:
        stmt = stmt.where(MetricBaseline.device_id.in_(device_ids))
    rows = (await session.execute(stmt)).scalars().all()
    return {
        (r.device_id, r.metric_name): {
            "p50": r.p50,
            "p25": r.p25,
            "p75": r.p75,
            "stddev": r.stddev,
            "sample_count": r.sample_count,
        }
        for r in rows
    }


async def detect_baseline_deviation(
    session: AsyncSession,
    device_id: int,
    metric_name: str,
    current_value: float,
    current_hour: int,
    sigma: float = 3.0,
    direction: str = "upper",
) -> dict | None:
    """单点偏离检测（供 API/调试使用）。无可用基线时返回 None。"""
    baseline = (await session.execute(
        select(MetricBaseline).where(
            MetricBaseline.device_id == device_id,
            MetricBaseline.metric_name == metric_name,
            MetricBaseline.hour_of_day == current_hour,
        )
    )).scalar_one_or_none()
    if baseline is None:
        return None

    violating, z = evaluate_baseline_violation(
        current_value, baseline.p50, baseline.stddev, sigma, direction
    )
    if z is None:
        return None

    if not violating:
        severity = None
    elif abs(z) > 4:
        severity = "critical"
    elif abs(z) > 3:
        severity = "major"
    else:
        severity = "minor"

    return {
        "severity": severity,
        "violating": violating,
        "z_score": z,
        "baseline_p50": baseline.p50,
        "baseline_stddev": baseline.stddev,
        "sample_count": baseline.sample_count,
        "current_value": current_value,
    }


async def get_device_baselines(session: AsyncSession, device_id: int) -> dict:
    """获取设备的所有基线记录，按 metric_name 分组。"""
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
