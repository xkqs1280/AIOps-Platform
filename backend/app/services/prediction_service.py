"""AI 预测引擎 — 基于模拟数据的网络设备预测服务。

在接入真实时序数据之前，所有预测使用可重现的模拟数据生成。
每个设备的模拟数据由 device_id 作为随机种子，保证同一设备每次计算结果一致。
"""

from app.models.p2_baseline import PredictionResult, MetricBaseline
from app.models.device import Device
from app.database import get_session
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert
import numpy as np
from datetime import datetime, timedelta, date


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _device_rng(device_id: int, seed_suffix: int = 0) -> np.random.Generator:
    """以 device_id 为种子创建可重现的随机数生成器。"""
    return np.random.default_rng(int(device_id) * 10000 + seed_suffix)


def _days_between(d1: date, d2: date) -> int:
    return (d2 - d1).days


# ---------------------------------------------------------------------------
# 1. 磁盘耗尽预测
# ---------------------------------------------------------------------------

async def predict_disk_exhaustion(session, device_id: int, days_history: int = 30) -> dict:
    """模拟磁盘使用趋势，预测达到 90%/95% 阈值的天数。

    Returns:
        dict: current_pct, trend, daily_growth_pct, days_to_90pct,
              days_to_95pct, estimated_exhaustion_date
    """
    rng = _device_rng(device_id, seed_suffix=1)

    # 生成 30 天模拟磁盘使用数据（微微上升趋势）
    base = round(float(rng.uniform(40.0, 70.0)), 1)
    trend_strength = rng.uniform(0.03, 0.12)
    noise = rng.normal(0, 0.5, days_history)
    days = np.arange(days_history)
    usage = base + trend_strength * days + noise
    usage = np.clip(usage, 30.0, 98.0)

    # 线性回归
    coeffs = np.polyfit(days, usage, deg=1)
    slope = coeffs[0]
    intercept = coeffs[1]
    current_pct = round(float(usage[-1]), 1)
    daily_growth_pct = round(slope, 4)

    # 推算到达阈值的天数
    def _days_until(threshold: float) -> int | None:
        if slope <= 0:
            return None
        remaining = threshold - current_pct
        if remaining <= 0:
            return 0
        return max(0, int(np.ceil(remaining / slope)))

    days_to_90pct = _days_until(90.0)
    days_to_95pct = _days_until(95.0)
    estimated_date = None
    if days_to_90pct is not None:
        estimated_date = date.today() + timedelta(days=days_to_90pct)

    # 残差标准差作为置信度参考
    predicted_values = intercept + slope * days
    residuals = usage - predicted_values
    rmse = float(np.sqrt(np.mean(residuals**2)))
    confidence = round(max(0.0, min(1.0, 1.0 - rmse / 10.0)), 2)

    # 存入 PredictionResult
    stmt = (
        insert(PredictionResult)
        .values(
            device_id=device_id,
            prediction_type="disk_exhaustion",
            metric_name="disk_usage_pct",
            current_value=current_pct,
            predicted_value=round(float(intercept + slope * (days_history + 30)), 1),
            predicted_date=estimated_date or date.today() + timedelta(days=999),
            confidence=confidence,
            details={
                "daily_growth_pct": daily_growth_pct,
                "days_to_90pct": days_to_90pct,
                "days_to_95pct": days_to_95pct,
                "rmse": round(rmse, 4),
                "simulated": True,
            },
            created_at=datetime.utcnow(),
        )
        .on_conflict_do_update(
            constraint="uq_prediction",
            set_={
                "current_value": func.excluded.current_value,
                "predicted_value": func.excluded.predicted_value,
                "predicted_date": func.excluded.predicted_date,
                "confidence": func.excluded.confidence,
                "details": func.excluded.details,
                "created_at": func.excluded.created_at,
            },
        )
    )
    await session.execute(stmt)
    await session.commit()

    print(f"[disk_exhaustion] device={device_id} current={current_pct}% slope={daily_growth_pct:.4f}/day days_to_90={days_to_90pct}")
    return {
        "current_pct": current_pct,
        "trend": "increasing" if slope > 0 else "stable",
        "daily_growth_pct": daily_growth_pct,
        "days_to_90pct": days_to_90pct,
        "days_to_95pct": days_to_95pct,
        "estimated_exhaustion_date": estimated_date.isoformat() if estimated_date else None,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# 2. 接口错误预测
# ---------------------------------------------------------------------------

async def predict_interface_errors(session, device_id: int, days_history: int = 30) -> dict:
    """模拟接口错误数据，检测错误率是否上升。

    Returns:
        dict: trend, current_rate, predicted_peak, risk_level
    """
    rng = _device_rng(device_id, seed_suffix=2)

    days = np.arange(days_history)
    # 偶尔的尖峰 + 可能的上升趋势
    base_rate = rng.uniform(0.0, 5.0)
    trend_coeff = rng.choice([0.0, 0.0, 0.02, 0.05, 0.1])  # 少数有上升趋势
    noise = rng.normal(0, 0.5, days_history)
    spikes = rng.binomial(1, 0.08, days_history) * rng.exponential(4.0, days_history)
    error_rate = base_rate + trend_coeff * days + noise + spikes
    error_rate = np.clip(error_rate, 0, None)

    coeffs = np.polyfit(days, error_rate, deg=1)
    slope = coeffs[0]
    current_rate = round(float(error_rate[-1]), 2)

    # 最近 7 天趋势
    recent_slope = float(np.polyfit(days[-7:], error_rate[-7:], deg=1)[0])
    trend = "increasing" if recent_slope > 0.05 else ("decreasing" if recent_slope < -0.05 else "stable")

    # 未来 7 天预测峰值
    forecast_days = np.arange(days_history, days_history + 7)
    forecast = coeffs[1] + coeffs[0] * forecast_days
    forecast = np.clip(forecast, 0, None)
    predicted_peak = round(float(np.max(forecast)), 2)

    # 风险等级
    if trend == "increasing" and current_rate > 5:
        risk_level = "high"
    elif trend == "increasing" or current_rate > 3:
        risk_level = "medium"
    else:
        risk_level = "low"

    confidence = round(rng.uniform(0.70, 0.95), 2)

    stmt = (
        insert(PredictionResult)
        .values(
            device_id=device_id,
            prediction_type="interface_error",
            metric_name="error_rate",
            current_value=current_rate,
            predicted_value=predicted_peak,
            predicted_date=date.today() + timedelta(days=7),
            confidence=confidence,
            details={
                "trend": trend,
                "recent_slope": round(recent_slope, 4),
                "risk_level": risk_level,
                "spike_count": int(np.sum(spikes > 2)),
                "simulated": True,
            },
            created_at=datetime.utcnow(),
        )
        .on_conflict_do_update(
            constraint="uq_prediction",
            set_={
                "current_value": func.excluded.current_value,
                "predicted_value": func.excluded.predicted_value,
                "predicted_date": func.excluded.predicted_date,
                "confidence": func.excluded.confidence,
                "details": func.excluded.details,
                "created_at": func.excluded.created_at,
            },
        )
    )
    await session.execute(stmt)
    await session.commit()

    print(f"[interface_errors] device={device_id} current_rate={current_rate} trend={trend} risk={risk_level}")
    return {
        "trend": trend,
        "current_rate": current_rate,
        "predicted_peak": predicted_peak,
        "risk_level": risk_level,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# 3. CPU 趋势预测
# ---------------------------------------------------------------------------

async def predict_cpu_trend(session, device_id: int, days_history: int = 30, days_forecast: int = 14) -> dict:
    """模拟 CPU 使用率数据（含昼夜模式），预测未来趋势。

    Returns:
        dict: trend, predicted_values, capacity_alert, recommendation
    """
    rng = _device_rng(device_id, seed_suffix=3)

    days = np.arange(days_history)
    base_cpu = rng.uniform(25.0, 60.0)
    trend_coeff = rng.choice([0.0, 0.05, 0.1, 0.15])  # 不同程度的上升
    amplitude = rng.uniform(3.0, 10.0)  # 昼夜振幅
    noise = rng.normal(0, 2.0, days_history)

    # 昼夜周期 sin(2π * day / 7)，7 天一周期的假节律
    diurnal = amplitude * np.sin(2 * np.pi * days / 7)
    cpu_data = base_cpu + trend_coeff * days + diurnal + noise
    cpu_data = np.clip(cpu_data, 5.0, 95.0)

    coeffs = np.polyfit(days, cpu_data, deg=1)
    slope = coeffs[0]
    current_avg = round(float(np.mean(cpu_data[-7:])), 1)
    trend = "increasing" if slope > 0.2 else ("decreasing" if slope < -0.2 else "stable")

    # 预测未来 days_forecast 天
    forecast_days = np.arange(days_history, days_history + days_forecast)
    predicted_values = []
    for fd in forecast_days:
        trend_val = coeffs[1] + coeffs[0] * fd
        diurnal_val = amplitude * np.sin(2 * np.pi * fd / 7)
        val = round(float(np.clip(trend_val + diurnal_val, 5.0, 95.0)), 1)
        predicted_values.append(val)

    # 容量告警：检查是否有连续 7 天预测值超过 80%
    over_80 = [v > 80.0 for v in predicted_values]
    capacity_alert = False
    streak = 0
    for flag in over_80:
        if flag:
            streak += 1
            if streak >= 7:
                capacity_alert = True
                break
        else:
            streak = 0

    recommendation = (
        "建议扩容或平衡负载，CPU 预测持续高位"
        if capacity_alert
        else ("CPU 呈上升趋势，建议关注" if trend == "increasing" else "CPU 状态正常，无需干预")
    )

    confidence = round(rng.uniform(0.65, 0.90), 2)

    stmt = (
        insert(PredictionResult)
        .values(
            device_id=device_id,
            prediction_type="cpu_trend",
            metric_name="cpu_usage",
            current_value=current_avg,
            predicted_value=round(float(np.mean(predicted_values)), 1),
            predicted_date=date.today() + timedelta(days=days_forecast),
            confidence=confidence,
            details={
                "trend": trend,
                "predicted_values": predicted_values,
                "capacity_alert": capacity_alert,
                "recommendation": recommendation,
                "simulated": True,
            },
            created_at=datetime.utcnow(),
        )
        .on_conflict_do_update(
            constraint="uq_prediction",
            set_={
                "current_value": func.excluded.current_value,
                "predicted_value": func.excluded.predicted_value,
                "predicted_date": func.excluded.predicted_date,
                "confidence": func.excluded.confidence,
                "details": func.excluded.details,
                "created_at": func.excluded.created_at,
            },
        )
    )
    await session.execute(stmt)
    await session.commit()

    print(f"[cpu_trend] device={device_id} current_avg={current_avg}% trend={trend} capacity_alert={capacity_alert}")
    return {
        "trend": trend,
        "current_avg": current_avg,
        "predicted_values": predicted_values,
        "capacity_alert": capacity_alert,
        "recommendation": recommendation,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# 4. 硬件健康检查（规则引擎）
# ---------------------------------------------------------------------------

async def check_hardware_health(session, device_id: int) -> dict:
    """基于规则的硬件健康检查，不依赖 ML。

    Returns:
        dict: power_status, fan_status, temperature_status, recent_restarts, overall
    """
    rng = _device_rng(device_id, seed_suffix=4)

    result = await session.execute(
        select(Device).where(Device.id == device_id)
    )
    device = result.scalar_one_or_none()

    # ---- 模拟硬件传感器数据 ----
    # 电源状态：90% 正常，10% 单个模块异常
    power_ok = rng.random() > 0.10
    power_status = "normal" if power_ok else "partial_failure"

    # 风扇状态：85% 正常，10% 降速，5% 异常
    fan_roll = rng.random()
    if fan_roll < 0.85:
        fan_status = "normal"
    elif fan_roll < 0.95:
        fan_status = "degraded"
    else:
        fan_status = "critical"

    # 温度：基于设备真实温度或模拟
    if device is not None and device.temperature is not None:
        temp = float(device.temperature)
    else:
        temp = round(rng.uniform(30.0, 75.0), 1)

    if temp < 40:
        temperature_status = "normal"
    elif temp < 60:
        temperature_status = "warning"
    else:
        temperature_status = "critical"

    # 最近重启：模拟 sysUpTime 检查
    uptime_seconds = rng.choice([rng.integers(86400, 30 * 86400),  # 正常
                                  rng.integers(300, 3600)])     # 最近重启过
    recent_restarts = uptime_seconds < 7200  # 2 小时内重启过视为异常

    # ---- 综合评估 ----
    issues = []
    if power_status != "normal":
        issues.append("电源模块异常")
    if fan_status != "normal":
        issues.append("风扇状态异常")
    if temperature_status == "critical":
        issues.append("设备温度高危")
    elif temperature_status == "warning":
        issues.append("设备温度偏高")
    if recent_restarts:
        issues.append("近期发生过重启")

    if not issues:
        overall = "healthy"
    elif len(issues) <= 1:
        overall = "warning"
    else:
        overall = "critical"

    # 设备存在与否可选
    device_info = {}
    if device is not None:
        device_info = {
            "device_name": device.name,
            "device_model": device.model,
            "device_type": device.device_type,
            "status": device.status,
            "cpu_usage": device.cpu_usage,
            "memory_usage": device.memory_usage,
        }

    print(f"[hardware_health] device={device_id} overall={overall} issues={issues}")
    return {
        "power_status": power_status,
        "fan_status": fan_status,
        "temperature": temp,
        "temperature_status": temperature_status,
        "recent_restarts": recent_restarts,
        "issues": issues,
        "overall": overall,
        "device_info": device_info,
    }


# ---------------------------------------------------------------------------
# 5. 批量预测入口
# ---------------------------------------------------------------------------

async def run_all_predictions(session, device_id: int | None = None) -> dict:
    """对全部设备（或指定设备）依次运行所有预测。

    Returns:
        dict: summary
    """
    if device_id is not None:
        stmt = select(Device).where(Device.id == device_id)
    else:
        stmt = select(Device)

    result = await session.execute(stmt)
    devices = result.scalars().all()

    summary = {
        "total_devices": len(devices),
        "disk_exhaustion": {"success": 0, "failed": 0},
        "interface_errors": {"success": 0, "failed": 0},
        "cpu_trend": {"success": 0, "failed": 0},
        "hardware_health": {"success": 0, "failed": 0},
        "errors": [],
    }

    for dev in devices:
        did = dev.id

        # 磁盘耗尽
        try:
            await predict_disk_exhaustion(session, did)
            summary["disk_exhaustion"]["success"] += 1
        except Exception as e:
            summary["disk_exhaustion"]["failed"] += 1
            summary["errors"].append({"device_id": did, "type": "disk_exhaustion", "error": str(e)})

        # 接口错误
        try:
            await predict_interface_errors(session, did)
            summary["interface_errors"]["success"] += 1
        except Exception as e:
            summary["interface_errors"]["failed"] += 1
            summary["errors"].append({"device_id": did, "type": "interface_errors", "error": str(e)})

        # CPU 趋势
        try:
            await predict_cpu_trend(session, did)
            summary["cpu_trend"]["success"] += 1
        except Exception as e:
            summary["cpu_trend"]["failed"] += 1
            summary["errors"].append({"device_id": did, "type": "cpu_trend", "error": str(e)})

        # 硬件健康（纯规则，不写库）
        try:
            await check_hardware_health(session, did)
            summary["hardware_health"]["success"] += 1
        except Exception as e:
            summary["hardware_health"]["failed"] += 1
            summary["errors"].append({"device_id": did, "type": "hardware_health", "error": str(e)})

    print(f"[run_all_predictions] done. devices={len(devices)} errors={len(summary['errors'])}")
    return summary
