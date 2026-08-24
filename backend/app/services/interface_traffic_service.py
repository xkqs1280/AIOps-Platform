"""设备接口流量采集 - 通过 SNMP 计算物理接口实时带宽利用率。

原理：
    相邻两次采样 ifInOctets / ifOutOctets 计数器差值 ÷ 时间间隔 × 8 = 每秒比特数，
    再除以接口带宽 ifSpeed 得到利用率。仅统计 up 状态且非虚拟/逻辑接口。
"""
import asyncio
import logging
import re

from app.services.discovery_service import snmp_walk

logger = logging.getLogger(__name__)

# RFC1213-MIB IF-MIB OIDs
IF_DESCR_OID = "1.3.6.1.2.1.2.2.1.2"    # ifDescr
IF_ADMIN_OID = "1.3.6.1.2.1.2.2.1.7"    # ifAdminStatus: 1=up 2=down
IF_OPER_OID = "1.3.6.1.2.1.2.2.1.8"     # ifOperStatus: 1=up 2=down
IF_SPEED_OID = "1.3.6.1.2.1.2.2.1.5"    # ifSpeed (bps)
IF_IN_OCT_OID = "1.3.6.1.2.1.2.2.1.10"  # ifInOctets (Counter32)
IF_OUT_OCT_OID = "1.3.6.1.2.1.2.2.1.16" # ifOutOctets (Counter32)

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

    返回项：{ifindex, name, speed, in_octets, out_octets, in_rate, out_rate,
             in_util, out_util, max_util}
    """

    async def _collect() -> list[dict]:
        # 1. 静态信息（4 张表并发抓取）
        descr_rows, speed_rows, admin_rows, oper_rows = await asyncio.gather(
            snmp_walk(ip, IF_DESCR_OID, community, timeout),
            snmp_walk(ip, IF_SPEED_OID, community, timeout),
            snmp_walk(ip, IF_ADMIN_OID, community, timeout),
            snmp_walk(ip, IF_OPER_OID, community, timeout),
        )
        descr = dict(descr_rows)
        speed_rows = dict(speed_rows)
        admin_rows = dict(admin_rows)
        oper_rows = dict(oper_rows)
        if not descr:
            raise TimeoutError(f"SNMP 读取接口信息失败（{ip}，community={community}）")

        # 2. 两次采样计数器（in/out 并发）
        async def sample():
            in_rows, out_rows = await asyncio.gather(
                snmp_walk(ip, IF_IN_OCT_OID, community, timeout),
                snmp_walk(ip, IF_OUT_OCT_OID, community, timeout),
            )
            return dict(in_rows), dict(out_rows)

        in1, out1 = await sample()
        await asyncio.sleep(sample_interval)
        in2, out2 = await sample()

        interval = sample_interval
        interfaces = []
        for oid, name in descr.items():
            idx = _if_index(oid, IF_DESCR_OID)
            if idx is None:
                continue
            if not _is_physical(name):
                continue
            # up 判定：admin=1 且 oper=1（兼容 up/up(1) 格式）
            admin_oid = f"{IF_ADMIN_OID}.{idx}"
            oper_oid = f"{IF_OPER_OID}.{idx}"
            if not _is_up(admin_rows.get(admin_oid)) or not _is_up(oper_rows.get(oper_oid)):
                continue

            speed_raw = speed_rows.get(f"{IF_SPEED_OID}.{idx}", "0")
            try:
                speed = int(speed_raw)
            except ValueError:
                speed = 0
            if speed <= 0:
                continue

            def _oct(d, key):
                try:
                    return int(d.get(key, "0"))
                except ValueError:
                    return 0

            key_in = f"{IF_IN_OCT_OID}.{idx}"
            key_out = f"{IF_OUT_OCT_OID}.{idx}"
            in_oct = _oct(in2, key_in) - _oct(in1, key_in)
            out_oct = _oct(out2, key_out) - _oct(out1, key_out)
            if in_oct < 0:
                in_oct = 0  # Counter 回绕，本采样窗忽略
            if out_oct < 0:
                out_oct = 0

            in_rate = in_oct * 8 / interval          # bps
            out_rate = out_oct * 8 / interval
            in_util = round(in_rate / speed * 100, 2) if speed else 0.0
            out_util = round(out_rate / speed * 100, 2) if speed else 0.0

            interfaces.append({
                "ifindex": idx,
                "name": name,
                "speed": speed,
                "in_rate": int(in_rate),
                "out_rate": int(out_rate),
                "in_util": in_util,
                "out_util": out_util,
                "max_util": round(max(in_util, out_util), 2),
            })

        return interfaces

    # 整体总超时 30 秒：8 次 SNMP 交互 + 采样间隔，避免慢设备拖挂前端请求
    return await asyncio.wait_for(_collect(), timeout=30)


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
