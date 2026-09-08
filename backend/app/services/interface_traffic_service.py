"""设备接口流量采集 - 通过 SNMP 计算物理接口实时带宽利用率。

原理：
    相邻两次采样 ifInOctets / ifOutOctets 计数器差值 ÷ 实际时间间隔 × 8 = 每秒比特数，
    再除以接口带宽（ifSpeed 优先，否则 ifHighSpeed×1e6）得到利用率。
    仅统计 up 状态且非虚拟/逻辑接口；**LACP 聚合成员口被排除**（其 ifInOctets 累计的
    是聚合层流量，会让利用率虚高数倍，物理上不可能超 100%）。
"""
import asyncio
import logging
import re
import time

from app.services.discovery_service import snmp_walk

logger = logging.getLogger(__name__)

# RFC1213-MIB IF-MIB OIDs
IF_DESCR_OID = "1.3.6.1.2.1.2.2.1.2"      # ifDescr
IF_ADMIN_OID = "1.3.6.1.2.1.2.2.1.7"      # ifAdminStatus: 1=up 2=down
IF_OPER_OID = "1.3.6.1.2.1.2.2.1.8"       # ifOperStatus: 1=up 2=down
IF_SPEED_OID = "1.3.6.1.2.1.2.2.1.5"      # ifSpeed (bps, 32位; >=4.29G 报 2^32-1 占位)
IF_IN_OCT_OID = "1.3.6.1.2.1.2.2.1.10"    # ifInOctets (Counter32)
IF_OUT_OCT_OID = "1.3.6.1.2.1.2.2.1.16"   # ifOutOctets (Counter32)

# IF-MIB::ifXTable (64 位扩展，必现于现代设备)
IF_HIGH_SPEED_OID = "1.3.6.1.2.1.31.1.1.1.15"   # ifHighSpeed, 单位 Mbps（4294967295 表示未知）
IF_HC_IN_OCT_OID = "1.3.6.1.2.1.31.1.1.1.6"     # ifHCInOctets (Counter64)
IF_HC_OUT_OCT_OID = "1.3.6.1.2.1.31.1.1.1.10"   # ifHCOutOctets (Counter64)

# IEEE 802.3ad (LACP) — 识别聚合成员口
DOT3AD_AGG_ATTACHED_OID = "1.2.840.10006.300.43.1.1.1.1.1.1"  # dot3adAggPortAttachedAggID: 0=未绑定, >0=聚合组 ifindex

# ifSpeed 占位值（IF-MIB 定义 2^32-1 表示"未知/超过本表量程"）
_IFSPEED_UNKNOWN = 4294967295

# 虚拟/逻辑接口关键词（华为/H3C/通用），命中即过滤
_VIRTUAL_KEYWORDS = (
    "vlanif", "vlan", "loopback", "inloopback", "null", "dialer",
    "tunnel", "eth-trunk", "bridge-aggregation", "stack-port",
    "virtual-", "virtual template", "m-group", "register", "inbond",
    "cellular", "nve", "sub-interface",
)

# 物理接口常见前缀（H3C/华为/思科/通用）
_PHYSICAL_PREFIX = (
    "ge", "gigabitethernet", "xe", "xgigabitethernet", "10ge", "25ge",
    "40ge", "100ge", "hundredgige", "fortyge", "ethernet", "fe",
    "fastethernet", "fge", "cge", "twe", "twentyfivegige", "fxgige",
    "et4/", "et5/", "xge", "x-e",
)


def _if_index(oid: str, base: str) -> str | None:
    """从列 OID 提取接口索引（最后一段）。"""
    if not oid.startswith(base):
        return None
    suffix = oid[len(base):].lstrip(".")
    if not suffix or "." in suffix:
        return None
    return suffix


def _is_up(value: str | None) -> bool:
    """net-snmp 状态值可能为 `1` / `up` / `up(1)`，统一判断为 up。"""
    if value is None:
        return False
    v = str(value).lower()
    return v.startswith("1") or v.startswith("up")


def _is_physical(if_name: str) -> bool:
    """判断是否为物理接口：非虚拟/逻辑，且名字看起来像物理口。"""
    name = (if_name or "").strip()
    low = name.lower()
    if not low:
        return False
    if any(k in low for k in _VIRTUAL_KEYWORDS):
        return False
    # 以物理前缀开头视为物理口；其余（如 BAGG 已过滤、Dialer 已过滤）默认保留，
    # 但排除纯数字/空等异常名。
    return True


async def collect_interface_traffic(
    ip: str,
    community: str = "aiops",
    sample_interval: float = 3.0,
    timeout: int = 8,
) -> list[dict]:
    """采集设备所有接口流量并返回列表（未过滤排序）。

    返回项：{ifindex, name, speed, in_rate, out_rate, in_util, out_util, max_util,
             speed_source, counter_64, actual_interval, lacp_member, util_capped}
    """

    async def _collect() -> list[dict]:
        # 1. 静态信息：5 张表并发（ifHighSpeed 仅在确实需要时按需补取，见 step 3）。
        #    注意并发路数控制：老设备对多路并行 getbulk 敏感，过多并发反而整体变慢。
        (descr_rows, speed_rows, admin_rows, oper_rows, lacp_rows) = await asyncio.gather(
            snmp_walk(ip, IF_DESCR_OID, community, timeout),
            snmp_walk(ip, IF_SPEED_OID, community, timeout),
            snmp_walk(ip, IF_ADMIN_OID, community, timeout),
            snmp_walk(ip, IF_OPER_OID, community, timeout),
            snmp_walk(ip, DOT3AD_AGG_ATTACHED_OID, community, timeout),
        )
        descr = dict(descr_rows)
        speed_d = dict(speed_rows)
        admin_d = dict(admin_rows)
        oper_d = dict(oper_rows)
        lacp_d = dict(lacp_rows)
        if not descr:
            raise TimeoutError(f"SNMP 读取接口信息失败（{ip}，community={community}）")

        # LACP 成员口集合：dot3adAggPortAttachedAggID != 0 的 port 视为聚合成员。
        # 此类接口的 ifInOctets 累计的是聚合层流量（H3C/华为/思科 共有行为），被
        # ifSpeed=物理 1G/10G 除后会让利用率虚高 2~4 倍甚至更多，物理上不可能超 100%。
        lacp_member_idx: set[str] = set()
        for oid, val in lacp_d.items():
            try:
                if int(val) != 0:
                    lacp_member_idx.add(oid.split(".")[-1])
            except (ValueError, AttributeError):
                continue

        def _to_int(v, default: int = 0) -> int:
            try:
                return int(v)
            except (ValueError, TypeError):
                return default

        # 2. 预扫描：收集 up 的物理接口（idx, name），并标记 ifSpeed 无效、需要
        #    ifHighSpeed 兜底的口。此阶段先把候选钉死，计算循环不再访问状态表。
        need_hs_idx: set[str] = set()
        candidates: list[tuple[str, str]] = []
        for oid, name in descr.items():
            idx = _if_index(oid, IF_DESCR_OID)
            if idx is None or not _is_physical(name):
                continue
            if idx in lacp_member_idx:
                continue  # 聚合成员口：流量计数器含聚合层数据，计算利用率无意义
            if not (
                _is_up(admin_d.get(f"{IF_ADMIN_OID}.{idx}"))
                and _is_up(oper_d.get(f"{IF_OPER_OID}.{idx}"))
            ):
                continue
            sp = _to_int(speed_d.get(f"{IF_SPEED_OID}.{idx}"))
            if sp <= 0 or sp == _IFSPEED_UNKNOWN:
                need_hs_idx.add(idx)
            candidates.append((idx, name))

        # 3. 确有口需要 ifHighSpeed 时才补取该表（表小、一次往返；多数设备跳过）
        high_speed_d: dict[str, str] = {}
        if need_hs_idx:
            high_speed_d = dict(await snmp_walk(ip, IF_HIGH_SPEED_OID, community, timeout))

        # 4. 两次计数器采样：64 位（ifHCIn/OutOctets）优先。第一采样同时充当 64 位
        #    支持性探测——非空即用 64 位；整表为空说明老设备不支持，回退 32 位重采。
        #    每轮采样仅 2 张表并发，把对设备的 SNMP 并发压力降到与旧版一致。
        async def sample(in_oid: str, out_oid: str) -> tuple[dict, dict, float]:
            t0 = time.monotonic()
            in_rows, out_rows = await asyncio.gather(
                snmp_walk(ip, in_oid, community, timeout),
                snmp_walk(ip, out_oid, community, timeout),
            )
            t1 = time.monotonic()
            return dict(in_rows), dict(out_rows), t1

        hc_in1, hc_out1, t1_end = await sample(IF_HC_IN_OCT_OID, IF_HC_OUT_OCT_OID)
        use_64 = bool(hc_in1 or hc_out1)
        if use_64:
            in1, out1 = hc_in1, hc_out1  # 第一样本即有效（64 位）
            await asyncio.sleep(sample_interval)
            in2, out2, t2_end = await sample(IF_HC_IN_OCT_OID, IF_HC_OUT_OCT_OID)
        else:
            # 老设备无 ifHC 表：回退 32 位计数器重新采样
            in1, out1, t1_end = await sample(IF_IN_OCT_OID, IF_OUT_OCT_OID)
            await asyncio.sleep(sample_interval)
            in2, out2, t2_end = await sample(IF_IN_OCT_OID, IF_OUT_OCT_OID)

        # 实际采样间隔 = 两次采样完成的真实时间差（消除 walk 耗时对 rate 的虚高）
        interval = max(t2_end - t1_end, 0.001)

        # 5. 逐口计算利用率
        interfaces = []
        for idx, name in candidates:
            # 速率基准：ifSpeed 优先（合法值），否则 ifHighSpeed×1e6
            speed_if = _to_int(speed_d.get(f"{IF_SPEED_OID}.{idx}"))
            speed_source = "ifSpeed"
            if speed_if <= 0 or speed_if == _IFSPEED_UNKNOWN:
                hs = _to_int(high_speed_d.get(f"{IF_HIGH_SPEED_OID}.{idx}"))
                if hs > 0 and hs != _IFSPEED_UNKNOWN:
                    speed_if = hs * 1_000_000  # Mbps → bps
                    speed_source = "ifHighSpeed"
                else:
                    continue

            key_in = f"{IF_HC_IN_OCT_OID}.{idx}" if use_64 else f"{IF_IN_OCT_OID}.{idx}"
            key_out = f"{IF_HC_OUT_OCT_OID}.{idx}" if use_64 else f"{IF_OUT_OCT_OID}.{idx}"
            in_oct = _to_int(in2.get(key_in)) - _to_int(in1.get(key_in))
            out_oct = _to_int(out2.get(key_out)) - _to_int(out1.get(key_out))
            # Counter 回绕：本采样窗忽略（避免跨倍回绕造成的虚高）
            if in_oct < 0:
                in_oct = 0
            if out_oct < 0:
                out_oct = 0

            in_rate = in_oct * 8 / interval          # bps
            out_rate = out_oct * 8 / interval
            in_util_raw = in_rate / speed_if * 100
            out_util_raw = out_rate / speed_if * 100
            # 物理上限 100%（即便速率/计数器异常也不可超 100）；标记 util_capped 供诊断
            in_util = round(min(max(in_util_raw, 0.0), 100.0), 2)
            out_util = round(min(max(out_util_raw, 0.0), 100.0), 2)
            in_util_capped = in_util_raw > 100.0
            out_util_capped = out_util_raw > 100.0

            interfaces.append({
                "ifindex": idx,
                "name": name,
                "speed": speed_if,
                "in_rate": int(in_rate),
                "out_rate": int(out_rate),
                "in_util": in_util,
                "out_util": out_util,
                "max_util": round(max(in_util, out_util), 2),
                "speed_source": speed_source,
                "counter_64": use_64,
                "actual_interval": round(interval, 3),
                "lacp_member": False,
                "util_capped": in_util_capped or out_util_capped,
            })

        # 把 LACP 成员口以独立标记追加（供上层诊断，但不参与 TOP10——max_util=0）
        for idx in lacp_member_idx:
            name_oid = f"{IF_DESCR_OID}.{idx}"
            if name_oid in descr:
                interfaces.append({
                    "ifindex": idx,
                    "name": descr[name_oid],
                    "speed": 0,
                    "in_rate": 0,
                    "out_rate": 0,
                    "in_util": 0.0,
                    "out_util": 0.0,
                    "max_util": 0.0,
                    "lacp_member": True,
                    "util_capped": False,
                })

        return interfaces

    # 整体总超时 40 秒：约 9~11 次 SNMP 交互 + 采样间隔，避免慢设备拖挂前端请求
    return await asyncio.wait_for(_collect(), timeout=40)


def _format_speed(bps: int) -> str:
    """把 bps 格式化为易读带宽（Gbps/Mbps）。"""
    if bps >= 1_000_000_000:
        return f"{bps / 1_000_000_000:.1f}G"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.0f}M"
    if bps >= 1_000:
        return f"{bps / 1_000:.0f}K"
    return f"{bps}B"


def format_rate(bps: int) -> str:
    """把 bps 格式化为易读速率。"""
    if bps >= 1_000_000_000:
        return f"{bps / 1_000_000_000:.2f}Gbps"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.2f}Mbps"
    if bps >= 1_000:
        return f"{bps / 1_000:.2f}Kbps"
    return f"{bps}bps"
