"""设备健康评分引擎 — 基于性能、稳定性、硬件、生命周期四维度的100分制评分。

评分模型：100 分减去四个维度的扣分（30+30+20+20）。
- 性能（max 30）：CPU/内存 P95、接口带宽
- 稳定性（max 30）：重启、接口抖动、P0/P1 告警
- 硬件（max 20）：电源、风扇、温度
- 生命周期（max 20）：保修、EOS、EOL
"""

from app.models.p2_baseline import DeviceHealthScore, PredictionResult, MetricBaseline
from app.models.device import Device
from app.models.alert import Alert
from app.database import get_session  # noqa: F401 — re-exported for callers
from sqlalchemy import select, func, and_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from datetime import datetime, timedelta, date  # noqa: F401 — date re-exported for callers

# 硬件温度默认阈值（°C），厂商阈值不可用时使用
DEFAULT_TEMP_THRESHOLD = 60.0


# ---------------------------------------------------------------------------
# 1. 单设备健康评分计算
# ---------------------------------------------------------------------------

async def calculate_health_score(session, device_id):
    """计算单个设备的健康评分并 upsert 到 DeviceHealthScore 表。

    流程：
      1. 获取设备信息（保修、EOS、EOL 日期，CPU/内存/温度等）
      2. 查询该设备近期告警（7 天 P0/P1、30 天全量）
      3. 按四维度计算扣分
      4. upsert 到 DeviceHealthScore（device_id 为主键）

    Returns:
        dict: total_score、各维度子分、扣分明细；设备不存在时返回 None。
    """
    # ── 获取设备 ──
    result = await session.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is None:
        return None

    now = datetime.utcnow()
    today = now.date()
    seven_days_ago = now - timedelta(days=7)
    thirty_days_ago = now - timedelta(days=30)

    # ================================================================
    # 性能维度（max 30 pts deduction）
    # ================================================================
    perf_items = []
    perf_deduction = 0

    cpu = device.cpu_usage or 0
    mem = device.memory_usage or 0

    # CPU 7-day P95
    if cpu > 90:
        perf_deduction += 20
        perf_items.append({"item": "cpu_p95", "value": cpu, "threshold": 90, "points": -20})
    elif cpu > 80:
        perf_deduction += 10
        perf_items.append({"item": "cpu_p95", "value": cpu, "threshold": 80, "points": -10})

    # Memory 7-day P95
    if mem > 85:
        perf_deduction += 10
        perf_items.append({"item": "memory_p95", "value": mem, "threshold": 85, "points": -10})

    # Interface bandwidth P95（从 MetricBaseline 获取各小时最大 P95）
    bw_result = await session.execute(
        select(func.max(MetricBaseline.p95)).where(
            and_(
                MetricBaseline.device_id == device_id,
                MetricBaseline.metric_name == "bandwidth_usage",
            )
        )
    )
    bandwidth_p95 = bw_result.scalar()
    if bandwidth_p95 is not None and bandwidth_p95 > 90:
        perf_deduction += 10
        perf_items.append({
            "item": "bandwidth_sustained",
            "value": float(bandwidth_p95),
            "threshold": 90,
            "points": -10,
        })

    perf_deduction = min(perf_deduction, 30)
    perf_score = 30 - perf_deduction

    # ================================================================
    # 稳定性维度（max 30 pts deduction）
    # ================================================================
    stab_items = []
    stab_deduction = 0

    # 30 天内重启次数（rule_name 包含 "restart"）
    restart_result = await session.execute(
        select(func.count(Alert.id)).where(
            and_(
                Alert.device_id == device_id,
                Alert.rule_name.ilike("%restart%"),
                Alert.triggered_at >= thirty_days_ago,
            )
        )
    )
    restart_count = restart_result.scalar() or 0
    restart_ded = min(restart_count * 5, 15)
    stab_deduction += restart_ded
    if restart_ded > 0:
        stab_items.append({"item": "restarts_30d", "count": restart_count, "points": -restart_ded})

    # 7 天内接口抖动次数（rule_name 包含 "flap"）
    flap_result = await session.execute(
        select(func.count(Alert.id)).where(
            and_(
                Alert.device_id == device_id,
                Alert.rule_name.ilike("%flap%"),
                Alert.triggered_at >= seven_days_ago,
            )
        )
    )
    flap_count = flap_result.scalar() or 0
    flap_ded = min(flap_count * 2, 10)
    stab_deduction += flap_ded
    if flap_ded > 0:
        stab_items.append({"item": "interface_flaps_7d", "count": flap_count, "points": -flap_ded})

    # 7 天内严重/重要告警数
    p0p1_result = await session.execute(
        select(func.count(Alert.id)).where(
            and_(
                Alert.device_id == device_id,
                Alert.severity.in_(["critical", "major"]),
                Alert.triggered_at >= seven_days_ago,
            )
        )
    )
    p0p1_count = p0p1_result.scalar() or 0
    p0p1_ded = min(p0p1_count * 2, 15)
    stab_deduction += p0p1_ded
    if p0p1_ded > 0:
        stab_items.append({"item": "p0p1_alerts_7d", "count": p0p1_count, "points": -p0p1_ded})

    stab_deduction = min(stab_deduction, 30)
    stab_score = 30 - stab_deduction

    # ================================================================
    # 硬件维度（max 20 pts deduction）
    # ================================================================
    hw_items = []
    hw_deduction = 0

    # 电源异常/冗余丢失（活跃告警，rule_name 包含 "power"）
    power_result = await session.execute(
        select(func.count(Alert.id)).where(
            and_(
                Alert.device_id == device_id,
                Alert.rule_name.ilike("%power%"),
                Alert.status == "active",
            )
        )
    )
    if (power_result.scalar() or 0) > 0:
        hw_deduction += 10
        hw_items.append({"item": "power_supply", "status": "abnormal", "points": -10})

    # 风扇故障/转速异常（活跃告警，rule_name 包含 "fan"）
    fan_result = await session.execute(
        select(func.count(Alert.id)).where(
            and_(
                Alert.device_id == device_id,
                Alert.rule_name.ilike("%fan%"),
                Alert.status == "active",
            )
        )
    )
    if (fan_result.scalar() or 0) > 0:
        hw_deduction += 5
        hw_items.append({"item": "fan", "status": "fault", "points": -5})

    # 温度超过厂商阈值
    temp = device.temperature
    if temp is not None and temp > DEFAULT_TEMP_THRESHOLD:
        hw_deduction += 5
        hw_items.append({
            "item": "temperature",
            "value": float(temp),
            "threshold": DEFAULT_TEMP_THRESHOLD,
            "points": -5,
        })

    hw_deduction = min(hw_deduction, 20)
    hw_score = 20 - hw_deduction

    # ================================================================
    # 生命周期维度（max 20 pts deduction）
    # ================================================================
    lc_items = []
    lc_deduction = 0

    # 保修过期
    if device.warranty_expire:
        w_date = (
            device.warranty_expire.date()
            if isinstance(device.warranty_expire, datetime)
            else device.warranty_expire
        )
        if w_date < today:
            lc_deduction += 8
            lc_items.append({"item": "warranty_expired", "date": w_date.isoformat(), "points": -8})

    # EOS 已到达
    if device.eos_date:
        e_date = (
            device.eos_date.date()
            if isinstance(device.eos_date, datetime)
            else device.eos_date
        )
        if e_date <= today:
            lc_deduction += 12
            lc_items.append({"item": "eos_reached", "date": e_date.isoformat(), "points": -12})

    # EOL 180 天内
    if device.eol_date:
        eol_date = (
            device.eol_date.date()
            if isinstance(device.eol_date, datetime)
            else device.eol_date
        )
        days_to_eol = (eol_date - today).days
        if 0 <= days_to_eol <= 180:
            lc_deduction += 10
            lc_items.append({
                "item": "eol_within_180d",
                "date": eol_date.isoformat(),
                "days_remaining": days_to_eol,
                "points": -10,
            })

    lc_deduction = min(lc_deduction, 20)
    lc_score = 20 - lc_deduction

    # ================================================================
    # 汇总 & 存储
    # ================================================================
    total_score = perf_score + stab_score + hw_score + lc_score

    details = {
        "performance": {"deduction": perf_deduction, "score": perf_score, "max": 30, "items": perf_items},
        "stability": {"deduction": stab_deduction, "score": stab_score, "max": 30, "items": stab_items},
        "hardware": {"deduction": hw_deduction, "score": hw_score, "max": 20, "items": hw_items},
        "lifecycle": {"deduction": lc_deduction, "score": lc_score, "max": 20, "items": lc_items},
    }

    # upsert：device_id 为主键，冲突时更新
    stmt = pg_insert(DeviceHealthScore).values(
        device_id=device_id,
        total_score=total_score,
        performance=perf_score,
        stability=stab_score,
        hardware=hw_score,
        lifecycle=lc_score,
        details=details,
        calculated_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["device_id"],
        set_={
            "total_score": stmt.excluded.total_score,
            "performance": stmt.excluded.performance,
            "stability": stmt.excluded.stability,
            "hardware": stmt.excluded.hardware,
            "lifecycle": stmt.excluded.lifecycle,
            "details": stmt.excluded.details,
            "calculated_at": stmt.excluded.calculated_at,
        },
    )
    await session.execute(stmt)
    await session.flush()

    return {
        "device_id": device_id,
        "total_score": total_score,
        "performance": perf_score,
        "stability": stab_score,
        "hardware": hw_score,
        "lifecycle": lc_score,
        "deductions": details,
        "calculated_at": now.isoformat(),
    }


# ---------------------------------------------------------------------------
# 2. 查询单设备健康评分
# ---------------------------------------------------------------------------

async def get_device_health(session, device_id):
    """从数据库获取设备最新健康评分及全部明细。

    Returns:
        dict: 健康评分详情；无记录时返回 None。
    """
    result = await session.execute(
        select(DeviceHealthScore).where(DeviceHealthScore.device_id == device_id)
    )
    hs = result.scalar_one_or_none()
    if hs is None:
        return None

    return {
        "device_id": hs.device_id,
        "total_score": hs.total_score,
        "performance": hs.performance,
        "stability": hs.stability,
        "hardware": hs.hardware,
        "lifecycle": hs.lifecycle,
        "details": hs.details,
        "calculated_at": hs.calculated_at.isoformat() if hs.calculated_at else None,
    }


# ---------------------------------------------------------------------------
# 3. 查询全部设备健康评分
# ---------------------------------------------------------------------------

async def get_all_health_scores(session, min_score=None):
    """获取全部设备健康评分，可按最低分过滤，关联设备名称。

    Args:
        min_score: 可选，仅返回 total_score >= min_score 的记录。

    Returns:
        list[dict]: 按总分升序排列（最差优先）。
    """
    query = (
        select(DeviceHealthScore, Device.name)
        .outerjoin(Device, DeviceHealthScore.device_id == Device.id)
    )
    if min_score is not None:
        query = query.where(DeviceHealthScore.total_score >= min_score)
    query = query.order_by(DeviceHealthScore.total_score.asc())

    result = await session.execute(query)
    rows = result.all()

    return [
        {
            "device_id": hs.device_id,
            "device_name": name,
            "total_score": hs.total_score,
            "performance": hs.performance,
            "stability": hs.stability,
            "hardware": hs.hardware,
            "lifecycle": hs.lifecycle,
            "details": hs.details,
            "calculated_at": hs.calculated_at.isoformat() if hs.calculated_at else None,
        }
        for hs, name in rows
    ]


# ---------------------------------------------------------------------------
# 4. 批量计算全部设备健康评分
# ---------------------------------------------------------------------------

async def calculate_all_health_scores(session):
    """为全部设备计算健康评分。

    Returns:
        int: 已处理的设备数量。
    """
    result = await session.execute(select(Device.id))
    device_ids = [row[0] for row in result.all()]

    count = 0
    for device_id in device_ids:
        await calculate_health_score(session, device_id)
        count += 1

    await session.commit()
    return count
