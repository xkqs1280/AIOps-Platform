"""告警规则评估引擎 - 为每台设备建立规则状态库，把真实采集指标与告警规则比对，触发/自动恢复告警。

背景：此前只有「设备不可达」告警会触发，CPU/内存/温度等规则从未评估过——
指标采集器每 60s 把真实值写入 device 字段，但没有引擎去和规则比对。

本引擎设计（"每台设备一张状态库"）：
    _state[(device_id, rule_id)] = {
        "violating": bool,          # 当前是否处于违规状态
        "violation_start": datetime|None,  # 违规开始时间（条件首次满足的时刻）
        "value": float|None,        # 最近一次评估值
        "extra": str,               # 附加信息（如 down 的接口名列表）
    }
每轮评估（跟随指标采集循环，每 60s 一次）：
  1. 加载所有启用规则，只处理本引擎支持的指标
  2. cpu_usage / memory_usage / temperature 直接读采集循环刚写入的 device 字段
  3. sys_uptime / if_oper_status 现场 SNMP 采集（重启检测 / 接口 Down 检测）
  4. 条件满足并持续 >= rule.duration 秒 → 创建告警（按 device+rule 去重，不重复刷屏）
  5. 条件恢复 → 自动把该 device+rule 的 active 告警置为 resolved
"""
import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device
from app.models.alert import Alert, AlertRule
from app.services.discovery_service import snmp_get
from app.services.credential_service import reveal_secret
from app.services.metrics_collector import snmp_walk

logger = logging.getLogger(__name__)

# ---- 本引擎支持的指标（其余如 bandwidth_usage/disk_usage 暂未采集，跳过） ----
SUPPORTED_METRICS = {
    "cpu_usage", "memory_usage", "temperature", "sys_uptime", "if_oper_status",
    "if_in_errors", "if_out_errors", "if_in_discards", "if_out_discards",
}

# ---- SNMP OID ----
SYS_UPTIME_OID = "1.3.6.1.2.1.1.3.0"        # sysUpTime (TimeTicks, 1/100 秒)
IF_ADMIN_STATUS_OID = "1.3.6.1.2.1.2.2.1.7"  # ifAdminStatus: 1=up 2=down
IF_OPER_STATUS_OID = "1.3.6.1.2.1.2.2.1.8"   # ifOperStatus: 1=up 2=down ...
IF_NAME_OID = "1.3.6.1.2.1.31.1.1.1.1"        # ifName
# 接口错包/丢弃计数器（RFC1213-MIB/IF-MIB，Counter32，需两次采样取差值计算速率）
IF_IN_ERRORS_OID = "1.3.6.1.2.1.2.2.1.14"    # ifInErrors
IF_OUT_ERRORS_OID = "1.3.6.1.2.1.2.2.1.20"   # ifOutErrors
IF_IN_DISCARDS_OID = "1.3.6.1.2.1.2.2.1.13"  # ifInDiscards
IF_OUT_DISCARDS_OID = "1.3.6.1.2.1.2.2.1.19" # ifOutDiscards

# 错包指标 -> 计数器 OID / 中文名
ERROR_METRIC_OID = {
    "if_in_errors": IF_IN_ERRORS_OID,
    "if_out_errors": IF_OUT_ERRORS_OID,
    "if_in_discards": IF_IN_DISCARDS_OID,
    "if_out_discards": IF_OUT_DISCARDS_OID,
}
ERROR_METRIC_LABEL = {
    "if_in_errors": "入向错包",
    "if_out_errors": "出向错包",
    "if_in_discards": "入向丢弃",
    "if_out_discards": "出向丢弃",
}

IF_DOWN = 2       # ifOperStatus down
IF_ADMIN_UP = 1   # ifAdminStatus up（管理上启用的接口才报 down，人为 down 不告警）

# 错包速率采样间隔（秒）：两次计数器采样取差
IF_ERR_SAMPLE_INTERVAL = 3.0

# 重启判定：当前 uptime 比上次明显下降（小于上次的 80%），视为重启
UPTIME_RESTART_RATIO = 0.8

# 触发后的冷却期：uptime 重启告警触发后，期间 uptime 增长不会立即撤销告警
UPTIME_COOLDOWN_SECONDS = 600

MAX_CONCURRENT = 10

# 每台设备每规则的状态库: (device_id, rule_id) -> state dict
_state: dict[tuple[int, int], dict] = {}
# uptime 上一次值（ticks）: device_id -> ticks
_uptime_prev: dict[int, int] = {}
# 接口上一次运行状态（oper）: (device_id, ifIndex) -> oper(int)，用于检测 up→down 转换
_if_prev_status: dict[tuple[int, int], int] = {}
# 接口 down 转换时刻: (device_id, ifIndex) -> datetime（仅记录「由 up 转为 down」的接口）
_if_down_since: dict[tuple[int, int], datetime] = {}
# 错包/丢弃计数器上次采样值: (device_id, ifIndex) -> {metric: count}（Counter 基数）
_if_err_prev: dict[tuple[int, int], dict[str, int]] = {}
# 错包违规接口起始时刻: (device_id, rule_id, ifIndex) -> datetime
_if_err_since: dict[tuple[int, int, int], datetime] = {}


def cleanup_device_state(device_id: int) -> None:
    """设备删除后清理该设备的全部内存状态（避免残留导致误判/内存增长）。"""
    for key in list(_state.keys()):
        if key[0] == device_id:
            _state.pop(key, None)
    _uptime_prev.pop(device_id, None)
    for key in list(_if_prev_status.keys()):
        if key[0] == device_id:
            _if_prev_status.pop(key, None)
    for key in list(_if_down_since.keys()):
        if key[0] == device_id:
            _if_down_since.pop(key, None)
    for key in list(_if_err_prev.keys()):
        if key[0] == device_id:
            _if_err_prev.pop(key, None)
    for key in list(_if_err_since.keys()):
        if key[0] == device_id:
            _if_err_since.pop(key, None)


def _to_num(val) -> float | None:
    """从 SNMP 返回字符串提取数值：'up(1)' -> 1, '12345' -> 12345, 'Timeticks: (123) ...' -> 123。"""
    if val is None:
        return None
    s = str(val).strip()
    m = re.search(r"\((-?\d+)\)", s)
    if m:
        return float(m.group(1))
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _check_condition(condition: str, value: float, threshold: float) -> bool:
    if value is None:
        return False
    if condition == "gt":
        return value > threshold
    if condition == "gte":
        return value >= threshold
    if condition == "lt":
        return value < threshold
    if condition == "lte":
        return value <= threshold
    if condition == "eq":
        return value == threshold
    if condition == "ne":
        return value != threshold
    return False


def _fmt_ticks(ticks: float) -> str:
    seconds = int(ticks) / 100
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f} 分钟"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f} 小时"
    return f"{hours / 24:.1f} 天"


def _device_metric_value(device: Device, metric: str) -> float | None:
    if metric == "cpu_usage":
        return device.cpu_usage
    if metric == "memory_usage":
        return device.memory_usage
    if metric == "temperature":
        return device.temperature
    return None


def _scalar_message(device: Device, rule: AlertRule, value: float) -> str:
    metric_label = {
        "cpu_usage": "CPU 利用率",
        "memory_usage": "内存利用率",
        "temperature": "温度",
    }.get(rule.metric, rule.metric)
    unit = "%" if rule.metric in ("cpu_usage", "memory_usage") else "°C" if rule.metric == "temperature" else ""
    return (
        f"设备 {device.name}({device.ip}) {metric_label} {value}{unit}，"
        f"超过阈值 {rule.threshold}{unit}（持续 {rule.duration}s），触发「{rule.name}」告警"
    )


def _if_index(oid: str) -> int | None:
    oid = oid.lstrip(".")
    parts = oid.split(".")
    try:
        return int(parts[-1])
    except (ValueError, IndexError):
        return None


async def _collect_if_status(device: Device, community: str) -> tuple[dict[int, tuple[int, int]], dict[int, str]]:
    """采集所有接口的 (adminStatus, operStatus)，返回 ({ifIndex: (admin, oper)}, {ifIndex: 接口名})。"""
    oper_rows = await snmp_walk(device.ip, IF_OPER_STATUS_OID, community, timeout=6)
    admin_rows = await snmp_walk(device.ip, IF_ADMIN_STATUS_OID, community, timeout=6)
    name_rows = await snmp_walk(device.ip, IF_NAME_OID, community, timeout=6)

    oper: dict[int, int] = {}
    for oid, val in oper_rows:
        idx = _if_index(oid)
        v = _to_num(val)
        if idx is not None and v is not None:
            oper[idx] = int(v)
    admin: dict[int, int] = {}
    for oid, val in admin_rows:
        idx = _if_index(oid)
        v = _to_num(val)
        if idx is not None and v is not None:
            admin[idx] = int(v)
    names: dict[int, str] = {}
    for oid, val in name_rows:
        idx = _if_index(oid)
        if idx is not None:
            names[idx] = val

    status: dict[int, tuple[int, int]] = {}
    for idx, o in oper.items():
        if idx in admin:  # 两张表都有才纳入，避免采集缺失误判
            status[idx] = (admin[idx], o)
    return status, names


def _compute_alerted_ifaces(
    device_id: int, if_status: dict[int, tuple[int, int]],
    duration: int, now: datetime,
) -> list[int]:
    """计算当前满足告警条件的接口（由 up 转为 down 且持续 >= duration）。

    状态库规则：
    - 接口首次见到：只记录基线（prev=当前值），不告警——「一直 down」的接口（如未接线端口）永不触发；
    - prev=up 且当前=down：记录 down 转换时刻，开始计时；
    - 转换 down 持续 >= duration：纳入告警集合；
    - 接口恢复 up / 管理 down：清除 down 计时，不再告警。
    """
    alerted: list[int] = []
    for idx, (admin, oper) in if_status.items():
        prev = _if_prev_status.get((device_id, idx))
        _if_prev_status[(device_id, idx)] = oper

        if admin != IF_ADMIN_UP:
            # 管理 down：人为关闭的接口，不告警并清除计时
            _if_down_since.pop((device_id, idx), None)
            continue

        if oper == IF_DOWN:
            if prev == 1:  # 由 up 转为 down：开始计时
                _if_down_since[(device_id, idx)] = now
            elif prev is None:
                # 首次见到且已 down：视为一直如此，只记基线，不告警
                pass
            ds = _if_down_since.get((device_id, idx))
            if ds is not None and (now - ds).total_seconds() >= duration:
                alerted.append(idx)
        else:
            # 接口恢复 up：清除 down 计时
            _if_down_since.pop((device_id, idx), None)
    return alerted


async def _collect_if_errors(device: Device, community: str) -> dict[int, dict[str, int]]:
    """采集所有接口的错包/丢弃计数器（两次采样取差值）。

    返回 {ifIndex: {metric: delta_count}}；Counter 回绕或首次见到的接口不计入。
    只采集本引擎涉及的 4 个错包指标，避免无谓 SNMP 开销。
    """
    oids = list(ERROR_METRIC_OID.values())
    metric_by_oid = {v: k for k, v in ERROR_METRIC_OID.items()}

    async def _sample() -> dict[int, dict[str, int]]:
        rows_by_metric: dict[str, list] = {}
        # 并发 walk 4 张计数器表
        results = await asyncio.gather(
            *[snmp_walk(device.ip, oid, community, timeout=6) for oid in oids],
            return_exceptions=True,
        )
        for oid, rows in zip(oids, results):
            if isinstance(rows, Exception):
                rows_by_metric[metric_by_oid[oid]] = []
                continue
            rows_by_metric[metric_by_oid[oid]] = rows
        out: dict[int, dict[str, int]] = {}
        for metric, rows in rows_by_metric.items():
            for oid, val in rows:
                idx = _if_index(oid)
                num = _to_num(val)
                if idx is not None and num is not None and num >= 0:
                    out.setdefault(idx, {})[metric] = int(num)
        return out

    sample1 = await _sample()
    await asyncio.sleep(IF_ERR_SAMPLE_INTERVAL)
    sample2 = await _sample()

    result: dict[int, dict[str, int]] = {}
    for idx, cur in sample2.items():
        prev = sample1.get(idx)
        if prev is None:
            continue  # 首次出现，等待下一轮建立基线
        delta = {}
        for metric, val in cur.items():
            old = prev.get(metric)
            if old is None:
                continue
            diff = val - old
            if diff < 0:
                continue  # Counter 回绕或设备重启，本轮忽略
            delta[metric] = diff
        if delta:
            result[idx] = delta
    return result


async def _eval_if_errors(
    db: AsyncSession, device: Device, rule: AlertRule,
    err_deltas: dict[int, dict[str, int]], if_names: dict[int, str],
) -> None:
    """接口错包/丢弃速率告警。

    速率 = 采样窗内差值 / IF_ERR_SAMPLE_INTERVAL（个/秒）。
    状态库：接口首次见到的计数器只建基线不告警；连续多轮速率超阈值且持续 >= duration 才告警。
    告警信息带接口名和实测速率；恢复后自动 resolve。
    """
    if rule.metric not in ERROR_METRIC_OID:
        return
    key = (device.id, rule.id)
    st = _state.get(key)
    if st is None:
        st = {"violating": False, "violation_start": None, "value": None, "extra": ""}
        _state[key] = st

    now = datetime.now(timezone.utc)
    metric = rule.metric
    label = ERROR_METRIC_LABEL.get(metric, metric)

    alerted_ifaces: list[tuple[int, float]] = []
    for idx, deltas in err_deltas.items():
        delta = deltas.get(metric)
        if delta is None:
            continue
        rate = delta / IF_ERR_SAMPLE_INTERVAL
        violating = _check_condition(rule.condition, rate, rule.threshold)
        state_key = (device.id, rule.id, idx)
        if violating:
            if state_key not in _if_err_since:
                _if_err_since[state_key] = now
            since = _if_err_since[state_key]
            if (now - since).total_seconds() >= rule.duration:
                alerted_ifaces.append((idx, rate))
        else:
            _if_err_since.pop(state_key, None)

    # 清理已消失接口的违规计时
    for state_key in list(_if_err_since.keys()):
        if state_key[0] == device.id and state_key[1] == rule.id and state_key[2] not in err_deltas:
            _if_err_since.pop(state_key, None)

    st["value"] = float(len(alerted_ifaces))
    if alerted_ifaces:
        st["violating"] = True
        parts = []
        for idx, rate in alerted_ifaces:
            nm = if_names.get(idx, f"ifIndex{idx}")
            parts.append(f"{nm} {rate:.0f}/s")
        extra = ", ".join(parts[:20])
        msg = (
            f"设备 {device.name}({device.ip}) 接口{label}速率异常："
            f"{extra}{' 等' if len(alerted_ifaces) > 20 else ''}，"
            f"共 {len(alerted_ifaces)} 个接口超过阈值 {rule.threshold}/s"
        )
        st["extra"] = msg
        await _ensure_alert(db, device, rule, msg)
    else:
        if st["violating"]:
            st["violating"] = False
            st["violation_start"] = None
            await _resolve_alert(db, device, rule.name)



async def _ensure_alert(db: AsyncSession, device: Device, rule: AlertRule, message: str):
    """创建告警（按 device+rule 去重）；已存在则仅更新消息。"""
    res = await db.execute(
        select(Alert).where(
            Alert.device_id == device.id,
            Alert.rule_name == rule.name,
            Alert.status == "active",
        )
    )
    existing = res.scalars().first()
    if existing is not None:
        if existing.message != message:
            existing.message = message
        return
    db.add(Alert(
        device_id=device.id,
        rule_name=rule.name,
        severity=rule.severity,
        message=message,
        status="active",
        triggered_at=datetime.now(timezone.utc),
    ))
    logger.warning(f"Alert triggered: {device.name}({device.ip}) {rule.name}: {message}")
    # 邮件告警（异步发送，失败不影响业务；同设备同规则 5 分钟防轰炸窗口）
    from app.services.mail_service import send_alert_email
    try:
        await send_alert_email(
            db,
            subject=f"[{rule.severity}] {rule.name} - {device.name}",
            body=(
                f"告警时间：{datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"设备名称：{device.name}\n"
                f"IP 地址：{device.ip}\n"
                f"规则名称：{rule.name}\n"
                f"严重级别：{rule.severity}\n"
                f"告警内容：{message}\n\n"
                f"—— AIOps 智能运维托管平台"
            ),
            dedup_key=f"rule:{device.id}:{rule.id}",
        )
    except Exception as e:
        logger.warning(f"alert email failed: {e}")


async def _resolve_alert(db: AsyncSession, device: Device, rule_name: str):
    """将该设备该规则的所有 active 告警置为 resolved。"""
    res = await db.execute(
        select(Alert).where(
            Alert.device_id == device.id,
            Alert.rule_name == rule_name,
            Alert.status == "active",
        )
    )
    now = datetime.now(timezone.utc)
    resolved = 0
    for alert in res.scalars().all():
        alert.status = "resolved"
        alert.resolved_at = now
        resolved += 1
    if resolved:
        logger.info(f"Alert resolved: {device.name}({device.ip}) {rule_name} x{resolved}")


async def _eval_scalar(db: AsyncSession, device: Device, rule: AlertRule, value: float | None):
    """CPU/内存/温度 等标量阈值规则的状态机评估。"""
    key = (device.id, rule.id)
    st = _state.get(key)
    if st is None:
        st = {"violating": False, "violation_start": None, "value": None}
        _state[key] = st
    st["value"] = value

    now = datetime.now(timezone.utc)
    violating = value is not None and _check_condition(rule.condition, value, rule.threshold)

    if violating:
        if not st["violating"]:
            st["violating"] = True
            st["violation_start"] = now
            logger.info(f"[{device.name}] {rule.name} 进入违规：{value} (阈值 {rule.threshold})")
        elif (now - st["violation_start"]).total_seconds() >= rule.duration:
            await _ensure_alert(db, device, rule, _scalar_message(device, rule, value))
    else:
        if st["violating"]:
            logger.info(f"[{device.name}] {rule.name} 恢复：{value}")
            st["violating"] = False
            st["violation_start"] = None
            await _resolve_alert(db, device, rule.name)


async def _eval_uptime(db: AsyncSession, device: Device, rule: AlertRule, value_ticks: float):
    """sys_uptime 重启检测：运行时间显著回退即视为重启，持续 rule.duration 后触发。"""
    key = (device.id, rule.id)
    st = _state.get(key)
    if st is None:
        st = {"violating": False, "violation_start": None, "value": None, "extra": ""}
        _state[key] = st
    st["value"] = value_ticks

    now = datetime.now(timezone.utc)
    prev = _uptime_prev.get(device.id)

    restart_detected = False
    if prev is not None and prev > 0 and value_ticks < prev * UPTIME_RESTART_RATIO:
        restart_detected = True
    _uptime_prev[device.id] = value_ticks

    if restart_detected:
        # 检测到重启：记录违规起点（重启后 uptime 会持续增长，无法依赖"持续违规"）
        if not st["violating"]:
            st["violating"] = True
            st["violation_start"] = now
            st["extra"] = f"运行时间从 {_fmt_ticks(prev)} 重置为 {_fmt_ticks(value_ticks)}"
            logger.warning(f"[{device.name}] 检测到疑似重启：prev={prev} now={value_ticks}")

    if st["violating"]:
        if (now - st["violation_start"]).total_seconds() >= rule.duration:
            msg = (
                f"设备 {device.name}({device.ip}) 疑似重启：{st['extra']}，"
                f"运行时间发生重置，请检查设备状态与业务连续性"
            )
            await _ensure_alert(db, device, rule, msg)
            # 触发后进入冷却期：期间 uptime 增长不撤销告警
            st["violating"] = False
            st["violation_start"] = None
            st["cooldown_until"] = now + timedelta(seconds=UPTIME_COOLDOWN_SECONDS)
    else:
        cooldown = st.get("cooldown_until")
        if cooldown and now < cooldown:
            return  # 冷却期内保持告警 active
        st.pop("cooldown_until", None)
        await _resolve_alert(db, device, rule.name)


async def _eval_if_status(
    db: AsyncSession, device: Device, rule: AlertRule,
    if_status: dict[int, tuple[int, int]], if_names: dict[int, str],
):
    """if_oper_status 接口 Down 检测：仅「由 up 转为 down」的接口告警；
    一直处于 down（如未接线端口）只建基线，永不触发。"""
    key = (device.id, rule.id)
    st = _state.get(key)
    if st is None:
        st = {"violating": False, "violation_start": None, "value": None, "extra": ""}
        _state[key] = st

    now = datetime.now(timezone.utc)
    alerted_idx = _compute_alerted_ifaces(device.id, if_status, rule.duration, now)
    st["value"] = float(len(alerted_idx))
    st["extra"] = ", ".join(if_names.get(i, f"ifIndex{i}") for i in alerted_idx[:20])

    if alerted_idx:
        st["violating"] = True
        name_str = st["extra"] + (" 等" if len(alerted_idx) > 20 else "")
        msg = (
            f"设备 {device.name}({device.ip}) 接口由 UP 转为 DOWN（管理状态 up）：{name_str}，"
            f"共 {len(alerted_idx)} 个接口"
        )
        await _ensure_alert(db, device, rule, msg)
    else:
        if st["violating"]:
            st["violating"] = False
            st["violation_start"] = None
            await _resolve_alert(db, device, rule.name)


async def _evaluate_device(db: AsyncSession, device: Device, rules: list[AlertRule]):
    """评估单台设备的全部支持规则。"""
    community = reveal_secret(device.snmp_community) or "aiops"

    # 现场采集一次 uptime / 接口状态（仅当存在对应规则时）
    uptime_ticks = None
    if any(r.metric == "sys_uptime" for r in rules):
        raw = await snmp_get(device.ip, SYS_UPTIME_OID, community, timeout=5)
        uptime_ticks = _to_num(raw)

    if_status: dict[int, tuple[int, int]] = {}
    if_names: dict[int, str] = {}
    if any(r.metric == "if_oper_status" for r in rules):
        if_status, if_names = await _collect_if_status(device, community)

    # 错包/丢弃计数器：仅当存在错包规则时采集一次（两次采样取差值）
    err_deltas: dict[int, dict[str, int]] = {}
    err_rules = [r for r in rules if r.metric in ERROR_METRIC_OID]
    if err_rules:
        err_deltas = await _collect_if_errors(device, community)
        # 复用接口名（与 if_oper_status 共享 ifName OID）
        if not if_names:
            name_rows = await snmp_walk(device.ip, IF_NAME_OID, community, timeout=6)
            for oid, val in name_rows:
                idx = _if_index(oid)
                if idx is not None:
                    if_names[idx] = val

    for rule in rules:
        metric = rule.metric
        if metric == "sys_uptime":
            if uptime_ticks is not None:
                await _eval_uptime(db, device, rule, uptime_ticks)
        elif metric == "if_oper_status":
            await _eval_if_status(db, device, rule, if_status, if_names)
        elif metric in ERROR_METRIC_OID:
            await _eval_if_errors(db, device, rule, err_deltas, if_names)
        else:
            # cpu/memory/temperature：直接使用采集循环刚写入的 device 字段
            value = _device_metric_value(device, metric)
            await _eval_scalar(db, device, rule, value)


async def evaluate_rules(db: AsyncSession, devices: list[Device] | None = None):
    """执行一轮规则评估（由指标采集循环每 60s 调用一次）。"""
    if devices is None:
        res = await db.execute(select(Device))
        devices = res.scalars().all()
    if not devices:
        return

    res = await db.execute(select(AlertRule).where(AlertRule.enabled.is_(True)))
    rules = [r for r in res.scalars().all() if r.metric in SUPPORTED_METRICS]
    if not rules:
        return

    # 清理已禁用/删除规则的残留状态，避免内存持续增长
    valid_rule_ids = {r.id for r in rules}
    for key in list(_state.keys()):
        if key[1] not in valid_rule_ids:
            _state.pop(key, None)

    # 离线设备由健康检测服务负责（设备不可达告警），跳过规则评估避免误报
    online_devices = [d for d in devices if d.status != "offline"]
    if not online_devices:
        return

    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async def _eval(d: Device):
        async with sem:
            try:
                await _evaluate_device(db, d, rules)
            except Exception as e:
                logger.error(f"Rule evaluate error {d.name}({d.ip}): {e}")

    await asyncio.gather(*[_eval(d) for d in online_devices])
    await db.commit()
