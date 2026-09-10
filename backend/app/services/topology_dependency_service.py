# -*- coding: utf-8 -*-
"""拓扑依赖推导：从拓扑连线自动推断设备之间的「上游 / 下游」关系。

用途 —— 告警收敛中的「拓扑依赖抑制」：上游设备不可达时，抑制其下挂设备的连带告警，
避免一次掉线刷出几十上百条（运维真正要看的只有"上游挂了"这一条）。

依赖**不再手工维护**（原 device_dependencies 表已停用）：用户在拓扑页画的连线
本身就是网络结构的表达，依赖直接由连线推导，不需要再维护第二份数据。

推导规则（保守优先：宁可不抑制，也不误抑制）

1. 设备类型层级，权重高的为上游：
   firewall > router > load_balancer > switch > wireless > server
   层级不同时方向明确，直接成立。

2. 同层级设备（典型：核心交换机与接入交换机都是 switch）：
   比较两端在拓扑中的**连接数（度数）**，连接多的一方更靠上游。
   但仅当差值 ≥ DEGREE_GAP 时才成立，否则视为对等互联，**不建立依赖**——
   方向判反会导致真实告警被压掉（漏报），代价远高于少抑制几条。

连线本身是无向的（TopologyLink 落库时已把 source/target 按 id 规范化，且两设备间唯一），
因此方向完全由上述规则决定，与用户画线时的先后顺序无关。

同一对设备只会产生一条依赖边，且规则本身构成全序（层级或度数），不会成环。
"""
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device
from app.models.topology_link import TopologyLink

logger = logging.getLogger(__name__)

# 设备类型层级权重：数值越大越靠上游（越接近网络出口）
DEVICE_TYPE_RANK: dict[str, int] = {
    "firewall": 60,
    "router": 50,
    "load_balancer": 40,
    "switch": 30,
    "wireless": 20,
    "server": 10,
}

# 未知 / 未填类型按交换机对待（中性位置）
DEFAULT_RANK = 30

# 同层级时，连接数差值达到该值才判定上下游方向（否则视为对等互联）
DEGREE_GAP = 2

TYPE_LABELS: dict[str, str] = {
    "firewall": "防火墙",
    "router": "路由器",
    "load_balancer": "负载均衡",
    "switch": "交换机",
    "wireless": "无线控制器",
    "server": "服务器",
}


def _type_label(device_type: str | None) -> str:
    return TYPE_LABELS.get(device_type or "", "未知类型")


def rank_of(device_type: str | None) -> int:
    """设备类型的上游权重（越大越靠上游）。"""
    return DEVICE_TYPE_RANK.get(device_type or "", DEFAULT_RANK)


async def derive_dependencies(db: AsyncSession) -> dict:
    """从拓扑连线推导「下游 → 上游」依赖。

    返回：
        {
          "deps": {下游 device_id: [上游 device_id, ...]},
          "details": [ {downstream_id, downstream_name, upstream_id,
                        upstream_name, reason, basis} , ... ],
          "peer_pairs": [ {a_id, a_name, b_id, b_name, reason}, ... ],  # 判定为对等、不建依赖的
        }
    """
    link_rows = (await db.execute(
        select(TopologyLink.source_device_id, TopologyLink.target_device_id)
    )).all()
    if not link_rows:
        return {"deps": {}, "details": [], "peer_pairs": []}

    degree: dict[int, int] = {}
    pairs: list[tuple[int, int]] = []
    seen_pairs: set[tuple[int, int]] = set()
    for src, tgt in link_rows:
        if src == tgt:
            continue
        key = (min(src, tgt), max(src, tgt))
        if key in seen_pairs:  # 同一对设备理论上唯一，这里再兜一层
            continue
        seen_pairs.add(key)
        pairs.append((src, tgt))
        degree[src] = degree.get(src, 0) + 1
        degree[tgt] = degree.get(tgt, 0) + 1

    if not pairs:
        return {"deps": {}, "details": [], "peer_pairs": []}

    dev_rows = (await db.execute(
        select(Device.id, Device.name, Device.ip, Device.device_type)
        .where(Device.id.in_(set(degree)))
    )).all()
    name_of: dict[int, str] = {r[0]: (r[1] or r[2] or str(r[0])) for r in dev_rows}
    type_of: dict[int, str] = {r[0]: (r[3] or "") for r in dev_rows}

    deps: dict[int, list[int]] = {}
    details: list[dict] = []
    peer_pairs: list[dict] = []

    for a, b in pairs:
        rank_a, rank_b = rank_of(type_of.get(a)), rank_of(type_of.get(b))
        down_id: int | None = None
        up_id: int | None = None
        reason = ""
        basis = ""

        if rank_a != rank_b:
            down_id, up_id = (a, b) if rank_a < rank_b else (b, a)
            basis = "type"
            reason = (
                f"{_type_label(type_of.get(down_id))}位于"
                f"{_type_label(type_of.get(up_id))}下游"
            )
        else:
            deg_a, deg_b = degree.get(a, 0), degree.get(b, 0)
            if abs(deg_a - deg_b) >= DEGREE_GAP:
                down_id, up_id = (a, b) if deg_a < deg_b else (b, a)
                basis = "degree"
                reason = (
                    f"同层级按连接数：{name_of.get(down_id, down_id)} 连接 {degree[down_id]} 条，"
                    f"{name_of.get(up_id, up_id)} 连接 {degree[up_id]} 条"
                )

        if down_id is None:
            peer_pairs.append({
                "a_id": a, "a_name": name_of.get(a, str(a)),
                "b_id": b, "b_name": name_of.get(b, str(b)),
                "reason": "同层级且连接数接近，按对等互联处理（不建立依赖）",
            })
            continue

        ups = deps.setdefault(down_id, [])
        if up_id in ups:
            continue
        ups.append(up_id)
        details.append({
            "downstream_id": down_id,
            "downstream_name": name_of.get(down_id, str(down_id)),
            "upstream_id": up_id,
            "upstream_name": name_of.get(up_id, str(up_id)),
            "basis": basis,
            "reason": reason,
        })

    if details:
        logger.debug("拓扑依赖推导完成：%d 条依赖，%d 对判为对等", len(details), len(peer_pairs))
    return {"deps": deps, "details": details, "peer_pairs": peer_pairs}


async def describe_dependencies(db: AsyncSession) -> dict:
    """供 API 输出：推导结果 + 上下游设备当前状态（便于前端高亮）。"""
    derived = await derive_dependencies(db)
    device_ids: set[int] = set()
    for d in derived["details"]:
        device_ids.add(d["downstream_id"])
        device_ids.add(d["upstream_id"])

    status_of: dict[int, str] = {}
    if device_ids:
        rows = (await db.execute(
            select(Device.id, Device.status).where(Device.id.in_(device_ids))
        )).all()
        status_of = {r[0]: (r[1] or "unknown") for r in rows}

    items = []
    for d in derived["details"]:
        item = dict(d)
        item["upstream_status"] = status_of.get(d["upstream_id"], "unknown")
        item["downstream_status"] = status_of.get(d["downstream_id"], "unknown")
        items.append(item)

    total_links = len(items) + len(derived["peer_pairs"])
    return {
        "dependencies": items,
        "peer_pairs": derived["peer_pairs"],
        "summary": {
            "dependency_count": len(items),
            "peer_count": len(derived["peer_pairs"]),
            "link_count": total_links,
        },
    }
