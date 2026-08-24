"""安全事件分析引擎：攻击趋势检测、IP信誉评分、关联规则。

提供以下能力：
1. 攻击事件统计聚合（按类别、严重级别、动作）
2. 攻击趋势分析（按天 × 类别）
3. 攻击突发检测（与历史基线对比）
4. Top 攻击源 / 目标 IP 排行
5. IP 信誉分类（ThreatIntel + 行为分析）
6. 关联规则引擎（横向移动、暴力破解、DDoS、数据外泄）
7. 威胁情报种子数据
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func, and_, extract
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.p3_security import SecurityEvent, ThreatIntel
from app.models.device import Device
from app.database import get_session  # noqa: F401 — re-exported for callers


# 安全事件类别
_EVENT_CATEGORIES = ("intrusion", "malware", "ddos", "policy", "anomaly", "audit")

# 严重级别
_SEVERITIES = ("critical", "high", "medium", "low", "info")


def _empty_category_map() -> dict:
    """返回全类别归零的字典。"""
    return {c: 0 for c in _EVENT_CATEGORIES}


def _empty_severity_map() -> dict:
    """返回全严重级别归零的字典。"""
    return {s: 0 for s in _SEVERITIES}


def _empty_day(date_str: str) -> dict:
    """返回单日空模板。"""
    entry: dict = {"date": date_str, "total": 0}
    entry.update(_empty_category_map())
    return entry


# ---------------------------------------------------------------------------
# 1. 攻击统计
# ---------------------------------------------------------------------------

async def get_attack_stats(session: AsyncSession, hours: int = 24) -> dict:
    """聚合过去 N 小时的安全事件统计。

    按事件类别和严重级别分组，并统计阻断/放行数量。

    Returns:
        {total_events, by_category, by_severity, blocked_count, allowed_count}
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)

    # 按类别聚合
    cat_result = await session.execute(
        select(SecurityEvent.event_category, func.count(SecurityEvent.id))
        .where(SecurityEvent.timestamp >= since)
        .group_by(SecurityEvent.event_category)
    )
    by_category = _empty_category_map()
    for category, count in cat_result.all():
        if category in by_category:
            by_category[category] = count

    # 按严重级别聚合
    sev_result = await session.execute(
        select(SecurityEvent.severity, func.count(SecurityEvent.id))
        .where(SecurityEvent.timestamp >= since)
        .group_by(SecurityEvent.severity)
    )
    by_severity = _empty_severity_map()
    for severity, count in sev_result.all():
        if severity in by_severity:
            by_severity[severity] = count

    # 按动作聚合（blocked/dropped → blocked_count, allowed/detected → allowed_count）
    action_result = await session.execute(
        select(SecurityEvent.action, func.count(SecurityEvent.id))
        .where(SecurityEvent.timestamp >= since)
        .group_by(SecurityEvent.action)
    )
    blocked_count = 0
    allowed_count = 0
    for action, count in action_result.all():
        if action in ("blocked", "dropped"):
            blocked_count += count
        elif action in ("allowed", "detected"):
            allowed_count += count

    return {
        "total_events": sum(by_category.values()),
        "by_category": by_category,
        "by_severity": by_severity,
        "blocked_count": blocked_count,
        "allowed_count": allowed_count,
    }


# ---------------------------------------------------------------------------
# 2. 攻击趋势
# ---------------------------------------------------------------------------

async def get_attack_trend(session: AsyncSession, days: int = 30) -> list[dict]:
    """获取过去 N 天的每日事件计数，按事件类别分组。

    无事件的日期也会填充零值，保证返回长度恒等于 *days*。

    Returns:
        list[dict]: 每日明细 [{date, total, intrusion, malware, ddos, ...}]
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    date_col = func.date(SecurityEvent.timestamp).label("d")
    result = await session.execute(
        select(date_col, SecurityEvent.event_category, func.count(SecurityEvent.id))
        .where(SecurityEvent.timestamp >= since)
        .group_by(date_col, SecurityEvent.event_category)
        .order_by(date_col)
    )

    daily_map: dict[str, dict] = {}
    for date_val, category, count in result.all():
        date_str = date_val.isoformat() if hasattr(date_val, "isoformat") else str(date_val)
        entry = daily_map.setdefault(date_str, _empty_day(date_str))
        if category in _EVENT_CATEGORIES:
            entry[category] = count
        entry["total"] += count

    # 填充无事件的日期
    trend = []
    for i in range(days):
        day = (now - timedelta(days=days - 1 - i)).date()
        date_str = day.isoformat()
        trend.append(daily_map.get(date_str, _empty_day(date_str)))

    return trend


# ---------------------------------------------------------------------------
# 3. 攻击突发检测
# ---------------------------------------------------------------------------

async def detect_attack_surge(
    session: AsyncSession,
    window_hours: int = 1,
    lookback_days: int = 7,
) -> dict:
    """将当前时间窗口的事件数与历史基线（过去 N 天同一小时的均值）对比。

    - current > baseline × 5 → "attack"
    - current > baseline × 2 → "suspicious"
    - 否则                     → "normal"

    Returns:
        {status, current_count, baseline_avg, ratio}
    """
    now = datetime.now(timezone.utc)
    current_start = now - timedelta(hours=window_hours)

    # 当前窗口事件数
    current_count = await session.scalar(
        select(func.count(SecurityEvent.id)).where(
            SecurityEvent.timestamp >= current_start
        )
    )
    current_count = current_count or 0

    # 基线：过去 lookback_days 中同一小时的事件数总和
    current_hour = now.hour
    baseline_since = now - timedelta(days=lookback_days)
    baseline_total = await session.scalar(
        select(func.count(SecurityEvent.id)).where(
            and_(
                SecurityEvent.timestamp >= baseline_since,
                SecurityEvent.timestamp < current_start,
                extract("hour", SecurityEvent.timestamp) == current_hour,
            )
        )
    )
    baseline_total = baseline_total or 0
    baseline_avg = baseline_total / lookback_days if lookback_days > 0 else 0.0

    if baseline_avg > 0:
        ratio = current_count / baseline_avg
    else:
        ratio = float(current_count) if current_count > 0 else 0.0

    if ratio > 5:
        status = "attack"
    elif ratio > 2:
        status = "suspicious"
    else:
        status = "normal"

    return {
        "status": status,
        "current_count": current_count,
        "baseline_avg": round(baseline_avg, 2),
        "ratio": round(ratio, 2),
    }


# ---------------------------------------------------------------------------
# 4. Top 攻击源 IP
# ---------------------------------------------------------------------------

async def get_top_attack_sources(
    session: AsyncSession,
    limit: int = 10,
    days: int = 7,
) -> list[dict]:
    """过去 N 天事件数最多的 Top N 源 IP。

    Returns:
        list[dict]: [{src_ip, count, event_types, last_seen}]
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    result = await session.execute(
        select(
            SecurityEvent.src_ip,
            func.count(SecurityEvent.id).label("cnt"),
            func.max(SecurityEvent.timestamp).label("last_seen"),
            func.array_agg(SecurityEvent.event_category.distinct()).label("types"),
        )
        .where(
            and_(
                SecurityEvent.timestamp >= since,
                SecurityEvent.src_ip.isnot(None),
            )
        )
        .group_by(SecurityEvent.src_ip)
        .order_by(func.count(SecurityEvent.id).desc())
        .limit(limit)
    )

    sources = []
    for row in result.all():
        event_types = [t for t in (row.types or []) if t is not None]
        sources.append({
            "src_ip": row.src_ip,
            "count": row.cnt,
            "event_types": event_types,
            "last_seen": row.last_seen.isoformat() if row.last_seen else None,
        })

    return sources


# ---------------------------------------------------------------------------
# 5. Top 目标 IP
# ---------------------------------------------------------------------------

async def get_top_targets(
    session: AsyncSession,
    limit: int = 10,
    days: int = 7,
) -> list[dict]:
    """过去 N 天事件数最多的 Top N 目标 IP。

    Returns:
        list[dict]: [{dst_ip, count, event_types}]
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    result = await session.execute(
        select(
            SecurityEvent.dst_ip,
            func.count(SecurityEvent.id).label("cnt"),
            func.array_agg(SecurityEvent.event_category.distinct()).label("types"),
        )
        .where(
            and_(
                SecurityEvent.timestamp >= since,
                SecurityEvent.dst_ip.isnot(None),
            )
        )
        .group_by(SecurityEvent.dst_ip)
        .order_by(func.count(SecurityEvent.id).desc())
        .limit(limit)
    )

    targets = []
    for row in result.all():
        event_types = [t for t in (row.types or []) if t is not None]
        targets.append({
            "dst_ip": row.dst_ip,
            "count": row.cnt,
            "event_types": event_types,
        })

    return targets


# ---------------------------------------------------------------------------
# 6. IP 信誉分类
# ---------------------------------------------------------------------------

async def classify_ip_reputation(session: AsyncSession, src_ip: str) -> dict:
    """对源 IP 进行信誉分类。

    优先查询 ThreatIntel 表；未命中时基于行为分析：
    - 24h 内触发 ≥5 台设备 → "suspicious"
    - 1h 内产生 ≥50 事件   → "scanner"
    - 否则                  → "unknown"

    Returns:
        {ip, reputation, confidence, reason}
    """
    # 1. 查询威胁情报
    ti_result = await session.execute(
        select(ThreatIntel).where(ThreatIntel.indicator == src_ip)
    )
    threat = ti_result.scalar_one_or_none()

    if threat is not None:
        if threat.threat_type in ("c2", "malware"):
            reputation = "malicious"
        elif threat.threat_type == "scanner":
            reputation = "scanner"
        else:
            reputation = "suspicious"
        return {
            "ip": src_ip,
            "reputation": reputation,
            "confidence": threat.confidence or 0,
            "reason": f"Matched threat intel: {threat.threat_type} (source: {threat.source})",
        }

    # 2. 行为分析
    now = datetime.now(timezone.utc)
    last_24h = now - timedelta(hours=24)
    last_1h = now - timedelta(hours=1)

    # 24h 内触发的不同设备数
    device_count = await session.scalar(
        select(func.count(func.distinct(SecurityEvent.device_id))).where(
            and_(
                SecurityEvent.src_ip == src_ip,
                SecurityEvent.timestamp >= last_24h,
            )
        )
    )
    device_count = device_count or 0

    if device_count >= 5:
        return {
            "ip": src_ip,
            "reputation": "suspicious",
            "confidence": 70,
            "reason": f"Triggered {device_count} devices in 24h",
        }

    # 1h 内事件数
    event_count = await session.scalar(
        select(func.count(SecurityEvent.id)).where(
            and_(
                SecurityEvent.src_ip == src_ip,
                SecurityEvent.timestamp >= last_1h,
            )
        )
    )
    event_count = event_count or 0

    if event_count >= 50:
        return {
            "ip": src_ip,
            "reputation": "scanner",
            "confidence": 80,
            "reason": f"Generated {event_count} events in 1h",
        }

    return {
        "ip": src_ip,
        "reputation": "unknown",
        "confidence": 0,
        "reason": "No threat intel match and no anomalous behavior detected",
    }


# ---------------------------------------------------------------------------
# 7. 关联规则引擎
# ---------------------------------------------------------------------------

async def run_correlation_rules(session: AsyncSession) -> list[dict]:
    """执行 4 条关联规则并返回告警。

    a. 横向移动：同一 src_ip 在 1h 内触发 ≥3 台不同设备 → critical
    b. 暴力破解：同一 dst_ip:dst_port 在 1h 内 ≥50 事件 → major
    c. DDoS 模式：同一 dst_ip 在 5min 内来自 ≥100 个不同 src_ip → critical
    d. 数据外泄：event_category=anomaly 且 dst_ip 非 RFC1918（24h 内）→ major

    Returns:
        list[dict]: [{rule_name, severity, description, related_ips, event_count, time_window}]
    """
    now = datetime.now(timezone.utc)
    alerts: list[dict] = []

    # ── a. 横向移动检测 ──
    one_hour_ago = now - timedelta(hours=1)
    lm_result = await session.execute(
        select(
            SecurityEvent.src_ip,
            func.count(func.distinct(SecurityEvent.device_id)).label("device_count"),
            func.count(SecurityEvent.id).label("event_count"),
            func.array_agg(Device.name.distinct()).label("device_names"),
        )
        .outerjoin(Device, SecurityEvent.device_id == Device.id)
        .where(
            and_(
                SecurityEvent.timestamp >= one_hour_ago,
                SecurityEvent.src_ip.isnot(None),
            )
        )
        .group_by(SecurityEvent.src_ip)
        .having(func.count(func.distinct(SecurityEvent.device_id)) >= 3)
    )
    for row in lm_result.all():
        device_names = [n for n in (row.device_names or []) if n is not None]
        name_list = ", ".join(device_names[:5]) if device_names else "unknown"
        alerts.append({
            "rule_name": "lateral_movement",
            "severity": "critical",
            "description": (
                f"Source IP {row.src_ip} triggered security events on "
                f"{row.device_count} devices ({name_list}) within 1 hour"
            ),
            "related_ips": [row.src_ip],
            "event_count": row.event_count,
            "time_window": "1h",
        })

    # ── b. 暴力破解检测 ──
    bf_result = await session.execute(
        select(
            SecurityEvent.dst_ip,
            SecurityEvent.dst_port,
            func.count(SecurityEvent.id).label("event_count"),
        )
        .where(
            and_(
                SecurityEvent.timestamp >= one_hour_ago,
                SecurityEvent.dst_ip.isnot(None),
            )
        )
        .group_by(SecurityEvent.dst_ip, SecurityEvent.dst_port)
        .having(func.count(SecurityEvent.id) >= 50)
    )
    for row in bf_result.all():
        alerts.append({
            "rule_name": "brute_force",
            "severity": "major",
            "description": (
                f"Brute force detected on {row.dst_ip}:{row.dst_port} "
                f"with {row.event_count} events in 1 hour"
            ),
            "related_ips": [row.dst_ip],
            "event_count": row.event_count,
            "time_window": "1h",
        })

    # ── c. DDoS 模式检测 ──
    five_min_ago = now - timedelta(minutes=5)
    ddos_result = await session.execute(
        select(
            SecurityEvent.dst_ip,
            func.count(func.distinct(SecurityEvent.src_ip)).label("src_ip_count"),
            func.count(SecurityEvent.id).label("event_count"),
        )
        .where(
            and_(
                SecurityEvent.timestamp >= five_min_ago,
                SecurityEvent.dst_ip.isnot(None),
            )
        )
        .group_by(SecurityEvent.dst_ip)
        .having(func.count(func.distinct(SecurityEvent.src_ip)) >= 100)
    )
    for row in ddos_result.all():
        alerts.append({
            "rule_name": "ddos_pattern",
            "severity": "critical",
            "description": (
                f"DDoS pattern detected on {row.dst_ip} from "
                f"{row.src_ip_count} unique source IPs in 5 minutes"
            ),
            "related_ips": [row.dst_ip],
            "event_count": row.event_count,
            "time_window": "5m",
        })

    # ── d. 数据外泄检测 ──
    # dst_ip 非 RFC1918（模拟：不以 10./172./192. 开头）且 event_category 为 anomaly
    twenty_four_hours_ago = now - timedelta(hours=24)
    exfil_result = await session.execute(
        select(
            SecurityEvent.dst_ip,
            func.count(SecurityEvent.id).label("event_count"),
            func.array_agg(SecurityEvent.src_ip.distinct()).label("src_ips"),
        )
        .where(
            and_(
                SecurityEvent.timestamp >= twenty_four_hours_ago,
                SecurityEvent.event_category == "anomaly",
                SecurityEvent.dst_ip.isnot(None),
                ~SecurityEvent.dst_ip.like("10.%"),
                ~SecurityEvent.dst_ip.like("172.%"),
                ~SecurityEvent.dst_ip.like("192.%"),
            )
        )
        .group_by(SecurityEvent.dst_ip)
    )
    for row in exfil_result.all():
        src_ips = [ip for ip in (row.src_ips or []) if ip is not None]
        related = [row.dst_ip] + src_ips[:5]
        alerts.append({
            "rule_name": "data_exfiltration",
            "severity": "major",
            "description": (
                f"Potential data exfiltration: anomaly events to external IP "
                f"{row.dst_ip} ({row.event_count} events in 24h)"
            ),
            "related_ips": related,
            "event_count": row.event_count,
            "time_window": "24h",
        })

    return alerts


# ---------------------------------------------------------------------------
# 8. 威胁情报种子数据
# ---------------------------------------------------------------------------

async def seed_threat_intel(session: AsyncSession) -> int:
    """插入示例威胁情报数据。

    - 5 个已知恶意 IP（僵尸网络 C2 服务器）
    - 5 个已知扫描器 IP（Shodan/ZoomEye 风格）
    - 3 个钓鱼域名

    使用 upsert（indicator + indicator_type 冲突时更新）。

    Returns:
        int: 插入/更新的记录数。
    """
    now = datetime.now(timezone.utc)

    records = [
        # ── 已知恶意 IP（僵尸网络 C2 服务器）──
        {"indicator": "185.220.101.45", "indicator_type": "ipv4", "threat_type": "c2",
         "confidence": 95, "source": "MISP", "tags": ["botnet", "c2", "tor-exit"]},
        {"indicator": "194.165.16.78", "indicator_type": "ipv4", "threat_type": "c2",
         "confidence": 92, "source": "微步", "tags": ["botnet", "c2"]},
        {"indicator": "23.129.64.210", "indicator_type": "ipv4", "threat_type": "c2",
         "confidence": 90, "source": "奇安信", "tags": ["botnet", "c2", "trojan"]},
        {"indicator": "45.155.205.233", "indicator_type": "ipv4", "threat_type": "malware",
         "confidence": 88, "source": "MISP", "tags": ["botnet", "emotet"]},
        {"indicator": "91.219.236.166", "indicator_type": "ipv4", "threat_type": "c2",
         "confidence": 94, "source": "微步", "tags": ["botnet", "c2", "rat"]},

        # ── 已知扫描器 IP ──
        {"indicator": "198.51.100.1", "indicator_type": "ipv4", "threat_type": "scanner",
         "confidence": 75, "source": "Shodan", "tags": ["scanner", "recon"]},
        {"indicator": "203.0.113.50", "indicator_type": "ipv4", "threat_type": "scanner",
         "confidence": 72, "source": "ZoomEye", "tags": ["scanner", "recon"]},
        {"indicator": "198.51.100.42", "indicator_type": "ipv4", "threat_type": "scanner",
         "confidence": 78, "source": "Shodan", "tags": ["scanner", "port-scan"]},
        {"indicator": "203.0.113.99", "indicator_type": "ipv4", "threat_type": "scanner",
         "confidence": 70, "source": "ZoomEye", "tags": ["scanner", "web-scan"]},
        {"indicator": "192.0.2.100", "indicator_type": "ipv4", "threat_type": "scanner",
         "confidence": 73, "source": "Censys", "tags": ["scanner", "recon"]},

        # ── 钓鱼域名 ──
        {"indicator": "secure-login-verify.com", "indicator_type": "domain", "threat_type": "phishing",
         "confidence": 96, "source": "微步", "tags": ["phishing", "credential-theft"]},
        {"indicator": "account-update-needed.net", "indicator_type": "domain", "threat_type": "phishing",
         "confidence": 94, "source": "奇安信", "tags": ["phishing", "banking"]},
        {"indicator": "invoice-payment-portal.org", "indicator_type": "domain", "threat_type": "phishing",
         "confidence": 92, "source": "MISP", "tags": ["phishing", "fraud"]},
    ]

    count = 0
    for record in records:
        stmt = pg_insert(ThreatIntel).values(
            indicator=record["indicator"],
            indicator_type=record["indicator_type"],
            threat_type=record["threat_type"],
            confidence=record["confidence"],
            source=record["source"],
            tags=record["tags"],
            first_seen=now,
            last_seen=now,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_threat_intel_indicator",
            set_={
                "threat_type": stmt.excluded.threat_type,
                "confidence": stmt.excluded.confidence,
                "source": stmt.excluded.source,
                "tags": stmt.excluded.tags,
                "last_seen": stmt.excluded.last_seen,
            },
        )
        await session.execute(stmt)
        count += 1

    await session.commit()
    return count
