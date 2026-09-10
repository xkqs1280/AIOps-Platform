# -*- coding: utf-8 -*-
"""告警收敛：拓扑依赖抑制 / 抖动抑制 / 风暴聚合。

动机：修复三类"告警刷屏"，它们都是误报之外最伤使用体验的问题。

1) 拓扑依赖抑制
   核心交换机掉线时，其下挂的所有接入设备会因为失去上联而同时告警，一次故障
   刷出几十上百条。运维要的是"上游挂了"这一条。
   依赖关系**由拓扑连线自动推导**（见 services/topology_dependency_service.py）：
   用户在拓扑页画的连线即网络结构，层级高的一端为上游，无需手工维护依赖表。
   上游处于离线（device.status == offline 或存在活动的「设备不可达」告警）时，
   下游产生的新告警标记为 suppressed，并记录是被哪台设备抑制的。
   拓扑中没有任何连线时该逻辑完全不生效，对存量部署零影响。

2) 抖动抑制
   链路瞬断（1~3 分钟）会让同一规则的告警反复 触发→恢复→触发。
   统计窗口内该 (设备, 规则) 的恢复次数，超过阈值即认定链路不稳定，
   新告警带上 flap_count 并在文案里说明，同时**不再重复推送通知**。

3) 风暴聚合
   单台设备在短窗口内新增告警数超阈值时，判定为告警风暴：
   非 critical 的新告警折叠为 suppressed，并维护一条「告警风暴」汇总告警，
   把被折叠的条数记在 aggregated_count 上。critical 永远放行，不会被风暴逻辑吞掉。

被抑制的告警仍然落库（status='suppressed'），在前端可单独筛选查看——
抑制是为了少打扰，不是为了让故障隐身。
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert
from app.models.device import Device
from app.services.topology_dependency_service import derive_dependencies

logger = logging.getLogger(__name__)

# 上游离线的判定依据：这两条规则代表"设备不可达/采集不到"
UPSTREAM_DOWN_RULES = ("设备不可达", "SNMP 采集异常")

# ---- 抖动抑制参数 ----
FLAP_WINDOW_SECONDS = 1800   # 统计窗口：30 分钟
FLAP_THRESHOLD = 3           # 窗口内恢复次数达到该值即判定为抖动

# ---- 风暴聚合参数 ----
STORM_WINDOW_SECONDS = 600   # 统计窗口：10 分钟
STORM_THRESHOLD = 15         # 单设备窗口内新增告警数超过该值判定为风暴
STORM_SUMMARY_RULE = "告警风暴"

# 严重级别排序（与 notify_channel.SEVERITY_ORDER 保持一致）
_SEV_ORDER = {"warning": 1, "minor": 2, "major": 3, "critical": 4}

# ---- 内存窗口（进程级；重启后自然清空，不影响正确性，只影响窗口起点）----
# (device_id, rule_name) -> 最近恢复时间戳列表
_resolve_history: dict[tuple[int, str], list[float]] = {}
# device_id -> 最近告警创建时间戳列表
_alert_history: dict[int, list[float]] = {}
# (device_id, rule_name) -> 抖动次数（用于写入 Alert.flap_count）
_flap_counts: dict[tuple[int, str], int] = {}


def _prune(ts_list: list[float], window: int, now: float) -> list[float]:
    return [t for t in ts_list if now - t <= window]


def record_resolution(device_id: int, rule_name: str) -> None:
    """告警恢复时调用，用于统计抖动。"""
    now = time.monotonic()
    key = (device_id, rule_name)
    hist = _prune(_resolve_history.get(key, []), FLAP_WINDOW_SECONDS, now)
    hist.append(now)
    _resolve_history[key] = hist


def record_alert_created(device_id: int) -> None:
    """告警创建时调用，用于统计风暴。"""
    now = time.monotonic()
    hist = _prune(_alert_history.get(device_id, []), STORM_WINDOW_SECONDS, now)
    hist.append(now)
    _alert_history[device_id] = hist


def flap_count_within_window(device_id: int, rule_name: str) -> int:
    """窗口内该 (设备, 规则) 的恢复次数。"""
    now = time.monotonic()
    key = (device_id, rule_name)
    hist = _prune(_resolve_history.get(key, []), FLAP_WINDOW_SECONDS, now)
    _resolve_history[key] = hist
    return len(hist)


def alert_count_within_window(device_id: int) -> int:
    """窗口内该设备新增告警数。"""
    now = time.monotonic()
    hist = _prune(_alert_history.get(device_id, []), STORM_WINDOW_SECONDS, now)
    _alert_history[device_id] = hist
    return len(hist)


def cleanup_device_state(device_id: int) -> None:
    """设备删除后清理其收敛状态。"""
    _alert_history.pop(device_id, None)
    for key in list(_resolve_history.keys()):
        if key[0] == device_id:
            _resolve_history.pop(key, None)
    for key in list(_flap_counts.keys()):
        if key[0] == device_id:
            _flap_counts.pop(key, None)


# ---------------------------------------------------------------------------
# 依赖关系上下文
# ---------------------------------------------------------------------------

async def build_dependency_context(db: AsyncSession) -> dict:
    """每轮评估前构建一次依赖上下文，避免逐条告警查库。

    依赖关系**由拓扑连线自动推导**（topology_dependency_service），不再读取手工配置的
    device_dependencies 表——用户在拓扑页画的连线就是网络结构，无需再维护第二份数据。

    返回：
        {
          "deps": {下游 device_id: [上游 device_id, ...]},
          "downstreams": {上游 device_id: [下游 device_id, ...]},
          "upstream_down": {上游 device_id: 原因文案},
        }
    """
    derived = await derive_dependencies(db)
    deps: dict[int, list[int]] = derived["deps"]

    downstreams: dict[int, list[int]] = {}
    for down_id, up_ids in deps.items():
        for up_id in up_ids:
            downstreams.setdefault(up_id, []).append(down_id)

    if not deps:
        return {"deps": {}, "downstreams": {}, "upstream_down": {}}

    # 上游候选中，哪些处于"不可达"：设备状态 offline 或存在活动告警
    upstream_ids = list(downstreams.keys())
    down: dict[int, str] = {}

    offline_rows = (await db.execute(
        select(Device.id, Device.name).where(
            Device.id.in_(upstream_ids), Device.status == "offline"
        )
    )).all()
    for dev_id, name in offline_rows:
        down[dev_id] = f"上游设备 {name} 处于离线状态"

    alert_rows = (await db.execute(
        select(Alert.device_id).where(
            Alert.device_id.in_(upstream_ids),
            Alert.status == "active",
            Alert.rule_name.in_(UPSTREAM_DOWN_RULES),
        ).group_by(Alert.device_id)
    )).all()
    if alert_rows:
        names = {
            r[0]: r[1]
            for r in (await db.execute(
                select(Device.id, Device.name).where(
                    Device.id.in_([row[0] for row in alert_rows])
                )
            )).all()
        }
        for (dev_id,) in alert_rows:
            down.setdefault(dev_id, f"上游设备 {names.get(dev_id, dev_id)} 不可达")

    return {"deps": deps, "downstreams": downstreams, "upstream_down": down}


def _dependency_suppression(ctx: dict | None, device: Device) -> tuple[int, str] | None:
    """返回 (上游设备ID, 抑制原因)；无抑制则 None。"""
    if not ctx:
        return None
    up_down = ctx.get("upstream_down") or {}
    if not up_down:
        return None
    for up_id in ctx.get("deps", {}).get(device.id, []):
        if up_id in up_down:
            return up_id, f"因{up_down[up_id]}，本设备告警已抑制"
    return None


# ---------------------------------------------------------------------------
# 抑制判定
# ---------------------------------------------------------------------------

def evaluate_suppression(
    device: Device,
    rule_name: str,
    severity: str,
    ctx: dict | None = None,
) -> dict:
    """判定一条即将创建的告警是否应被抑制。

    返回：
        {
          "suppressed": bool,
          "reason": str | None,
          "by_device_id": int | None,
          "flap_count": int,
          "notify": bool,      # 是否推送通知
        }
    """
    result = {"suppressed": False, "reason": None, "by_device_id": None,
              "flap_count": 0, "notify": True}

    # 1) 拓扑依赖抑制（最高优先级）
    dep = _dependency_suppression(ctx, device)
    if dep is not None:
        up_id, reason = dep
        result.update(suppressed=True, by_device_id=up_id, reason=reason, notify=False)
        return result

    sev = _SEV_ORDER.get((severity or "").lower(), 2)

    # 2) 抖动抑制：只压通知，不压告警本身（告警仍需可见）
    flaps = flap_count_within_window(device.id, rule_name)
    if flaps >= FLAP_THRESHOLD:
        result["flap_count"] = flaps
        result["notify"] = False
        _flap_counts[(device.id, rule_name)] = flaps

    # 3) 风暴聚合：非 critical 才折叠
    if severity != "critical":
        count = alert_count_within_window(device.id)
        if count >= STORM_THRESHOLD:
            result.update(
                suppressed=True,
                reason=(
                    f"告警风暴：{STORM_WINDOW_SECONDS // 60} 分钟内本设备已产生 {count} 条告警，"
                    f"非紧急告警已折叠"
                ),
                notify=False,
            )
        return result

    return result


async def ensure_storm_summary(db: AsyncSession, device: Device, count: int) -> None:
    """维护设备的「告警风暴」汇总告警：把被折叠的条数累加进去。"""
    now = datetime.now(timezone.utc)
    existing = (await db.execute(
        select(Alert).where(
            Alert.device_id == device.id,
            Alert.rule_name == STORM_SUMMARY_RULE,
            Alert.status == "active",
        )
    )).scalars().first()

    if existing is None:
        db.add(Alert(
            device_id=device.id,
            rule_name=STORM_SUMMARY_RULE,
            severity="major",
            message=(
                f"设备 {device.name}({device.ip}) 在 {STORM_WINDOW_SECONDS // 60} 分钟内产生 "
                f"{count} 条告警，已触发告警风暴折叠。非紧急告警被合并，请优先排查该设备根因。"
            ),
            status="active",
            triggered_at=now,
            aggregated_count=count,
        ))
        logger.warning("Storm summary created for %s(%s): %s alerts", device.name, device.ip, count)
    else:
        existing.aggregated_count = max(existing.aggregated_count or 0, count)
        existing.message = (
            f"设备 {device.name}({device.ip}) 在 {STORM_WINDOW_SECONDS // 60} 分钟内产生 "
            f"{count} 条告警，已触发告警风暴折叠。非紧急告警被合并，请优先排查该设备根因。"
        )


async def resolve_storm_summary(db: AsyncSession, device_id: int) -> None:
    """风暴窗口内不再有新告警时恢复汇总告警。"""
    rows = (await db.execute(
        select(Alert).where(
            Alert.device_id == device_id,
            Alert.rule_name == STORM_SUMMARY_RULE,
            Alert.status == "active",
        )
    )).scalars().all()
    now = datetime.now(timezone.utc)
    for a in rows:
        a.status = "resolved"
        a.resolved_at = now


async def resolve_suppressed_alerts(db: AsyncSession, device_id: int) -> int:
    """上游恢复后，把该设备因依赖被抑制的告警恢复掉（后续如需告警会重新创建）。"""
    rows = (await db.execute(
        select(Alert).where(
            Alert.device_id == device_id,
            Alert.status == "suppressed",
            Alert.suppressed_by_device_id.isnot(None),
        )
    )).scalars().all()
    now = datetime.now(timezone.utc)
    for a in rows:
        a.status = "resolved"
        a.resolved_at = now
        a.suppress_reason = (a.suppress_reason or "") + "（上游已恢复）"
    if rows:
        logger.info("Resolved %s dependency-suppressed alerts for device %s", len(rows), device_id)
    return len(rows)
