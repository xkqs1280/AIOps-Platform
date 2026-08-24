"""SNMP 设备发现服务 - 通过 SNMP 查询设备基本信息。

基于 pysnmp 6（asyncio 原生），不再依赖 net-snmp CLI，跨平台纯 Python。
对外接口不变：snmp_get / snmp_walk（与旧 net-snmp 版本返回值格式兼容）。
"""
import asyncio
import logging
import re

from pyasn1.type.univ import ObjectIdentifier as _Pyasn1OID
from pyasn1.type.univ import OctetString as _Pyasn1Octets
from pysnmp.hlapi.asyncio import (
    SnmpEngine,
    CommunityData,
    ContextData,
    ObjectType,
    ObjectIdentity,
    getCmd,
    walkCmd,
    bulkWalkCmd,
    UdpTransportTarget,
)
from pysnmp.proto.rfc1902 import (
    Integer32,
    Counter32,
    Counter64,
    Gauge32,
    IpAddress,
    TimeTicks,
)
from pysnmp.proto.rfc1905 import NoSuchInstance, NoSuchObject

logger = logging.getLogger(__name__)

# 全局复用 SNMP engine，避免每请求重建；信号量限制并发（与旧版子进程并发等价）
_snmp_engine = SnmpEngine()
_snmp_semaphore = asyncio.Semaphore(20)

# CJK 统一表意文字区间（含扩展 A/B 常见设备中文名）
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
# ASCII 控制字符（排除合法换行/回车/制表符）
_CTRL_CHARS = "".join(chr(c) for c in range(32) if c not in (9, 10, 13))


def _try_decode_cn(raw: bytes) -> str | None:
    """尝试把字节流解码为中文文本。

    设备（H3C/华为等）的 sysName、接口描述可能是 GBK/GB18030 或 UTF-8
    编码的中文。按 net-snmp 惯例非 ASCII 会输出 Hex-STRING，但中文名
    应当还原为可读文本。仅当解码结果同时满足：
      - 包含至少一个 CJK 汉字
      - 不含控制字符（换行/回车/制表符除外）
    才接受；否则返回 None（由调用方转 Hex-STRING）。
    """
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if not _CJK_RE.search(text):
            continue
        if any(c in _CTRL_CHARS for c in text):
            continue
        return text
    return None


def _value_to_str(val) -> str:
    """把 pysnmp/pyasn1 值对象转成字符串，兼容 net-snmp 文本格式的数值部分。

    - 数字类（Integer/Gauge/Counter/TimeTicks）→ 十进制数字（net-snmp 的
      `up(1)` 枚举文本会变成纯数字 `1`，下游已兼容两种形式）；
    - OctetString 可打印 ASCII → 文本；
    - OctetString 可解码为中文（GBK/GB18030/UTF-8）→ 中文文本；
      H3C/华为等设备 sysName/接口描述可能是 GBK 编码的中文，若按
      net-snmp 旧逻辑会误转成 Hex-STRING `xx xx` 存入数据库；
    - 其余不可打印字节 → 模拟 net-snmp Hex-STRING `xx xx`；
    - noSuchInstance/noSuchObject → 空串（视为无值）。
    """
    if isinstance(val, (NoSuchInstance, NoSuchObject)):
        return ""
    if isinstance(val, _Pyasn1Octets):
        raw = val.asOctets()
        # 允许 \t(9) \n(10) \r(13) 及可打印 ASCII 视为文本（net-snmp STRING 含换行也输出文本）
        if all(b in (9, 10, 13) or 32 <= b < 127 for b in raw):
            return val.prettyPrint()
        # 尝试解码为中文：GBK/GB18030 是设备常见中文编码，其次 UTF-8。
        # 仅当解码结果含 CJK 统一表意文字且无可疑控制字符时才接受，避免误判二进制。
        decoded = _try_decode_cn(raw)
        if decoded is not None:
            return decoded
        # 含二进制：模拟 net-snmp Hex-STRING 输出
        return " ".join(f"{b:02X}" for b in raw)
    if isinstance(val, (Integer32, Counter32, Counter64, Gauge32, TimeTicks)):
        return str(int(val))
    if isinstance(val, IpAddress):
        return str(val)
    if isinstance(val, _Pyasn1OID):
        return val.prettyPrint()
    return str(val)


def _oid_to_str(oid_obj) -> str:
    """数字 OID（无前导点），与旧版 `snmpwalk -On` 输出一致。"""
    return oid_obj.getOid().prettyPrint().lstrip(".")


def _oid_tuple(oid: str) -> tuple:
    """数字字符串 OID → 数字元组，绕过 pysnmp 的 MIB 名解析（显著提速）。"""
    return tuple(int(x) for x in oid.split("."))


def _transport(ip: str, timeout: int) -> UdpTransportTarget:
    return UdpTransportTarget((ip, 161), timeout=timeout, retries=0)


async def snmp_get(ip: str, oid: str, community: str = "aiops", timeout: int = 5) -> str | None:
    """通过 SNMPv2c GET 查询单个 OID，返回字符串值；无值/异常返回 None。"""
    async with _snmp_semaphore:
        try:
            errorIndication, errorStatus, errorIndex, varBinds = await asyncio.wait_for(
                getCmd(
                    _snmp_engine,
                    CommunityData(community),
                    _transport(ip, timeout),
                    ContextData(),
                    ObjectType(ObjectIdentity(_oid_tuple(oid))),
                ),
                timeout + 3,
            )
        except Exception:
            return None
    if errorIndication or errorStatus or not varBinds:
        return None
    value = _value_to_str(varBinds[0][1])
    return value or None


class _BulkUnsupported(Exception):
    """设备不支持 GETBULK 时抛出，回退 GETNEXT 逐条 walk。"""


async def snmp_walk(ip: str, oid: str, community: str = "aiops", timeout: int = 5) -> list[tuple[str, str]]:
    """遍历指定 OID 子树，返回 [(oid, value_str), ...]；失败返回空列表。

    优先 GETBULK（批量、快），设备不支持时自动回退 GETNEXT 逐条 walk。
    """

    def _append(rows, oid_obj, val):
        val_str = _value_to_str(val)
        if not val_str:
            return
        rows.append((_oid_to_str(oid_obj), val_str))

    async def _collect_bulk() -> list[tuple[str, str]]:
        rows = []
        async for errorIndication, errorStatus, errorIndex, varBinds in bulkWalkCmd(
            _snmp_engine,
            CommunityData(community),
            _transport(ip, timeout),
            ContextData(),
            0,
            50,
            ObjectType(ObjectIdentity(_oid_tuple(oid))),
            lexicographicMode=False,
        ):
            if errorIndication or errorStatus:
                raise _BulkUnsupported()
            for oid_obj, val in varBinds:
                _append(rows, oid_obj, val)
        return rows

    async def _collect_next() -> list[tuple[str, str]]:
        rows = []
        async for errorIndication, errorStatus, errorIndex, varBinds in walkCmd(
            _snmp_engine,
            CommunityData(community),
            _transport(ip, timeout),
            ContextData(),
            ObjectType(ObjectIdentity(_oid_tuple(oid))),
            lexicographicMode=False,
        ):
            if errorIndication or errorStatus:
                break
            for oid_obj, val in varBinds:
                _append(rows, oid_obj, val)
        return rows

    total_timeout = max(timeout * 3, 15)
    async with _snmp_semaphore:
        try:
            try:
                rows = await asyncio.wait_for(_collect_bulk(), total_timeout)
            except _BulkUnsupported:
                rows = await asyncio.wait_for(_collect_next(), total_timeout)
        except Exception:
            return []

    # 兼容旧版：对叶子 OID（如 sysDescr.0）walk 会因 GETNEXT 语义返回 0 条，
    # 此时回退单点 GET 补一条，与 net-snmp `snmpwalk oid.0` 行为一致。
    if not rows and oid.endswith(".0"):
        val = await snmp_get(ip, oid, community, timeout)
        if val:
            rows.append((oid.lstrip("."), val))
    return rows



def _parse_h3c_model(sys_descr: str) -> str | None:
    """从 H3C sysDescr 中提取产品型号（跳过软件名 'H3C Comware Platform'）。

    典型:
      "H3C S6850"
      "H3C SecPath F1090"
      "H3C WX5540H-HCL"
      "H3C MSR36-20"
    """
    if not sys_descr:
        return None
    m = re.search(
        r"(SecPath\s+\S+|S\d+[-\w]+|MSR\d+[-\w]+|SR\d+[-\w]+|"
        r"WX\d+[-\w]+|WAC\d+[-\w]+|LS\d+[-\w]+|F\d+[-\w]+)",
        sys_descr,
    )
    if not m:
        return None
    return m.group(1).strip()


def _h3c_device_type(model: str | None) -> str:
    """根据 H3C 型号前缀推断设备类型。"""
    if not model:
        return "switch"
    up = model.upper()
    if up.startswith("MSR") or up.startswith("SR"):
        return "router"
    if up.startswith("WX") or up.startswith("WAC") or "AC" in up:
        return "wireless"
    if up.startswith("F") or "SECPATH" in up:
        return "firewall"
    if up.startswith("L"):
        return "load_balancer"
    return "switch"


async def discover_device(ip: str, community: str = "aiops") -> dict:
    """通过 SNMP 发现单台设备信息（厂商/型号/序列号/类型）"""
    sys_descr, sys_name, serial_rows = await asyncio.gather(
        snmp_get(ip, "1.3.6.1.2.1.1.1.0", community),   # sysDescr
        snmp_get(ip, "1.3.6.1.2.1.1.5.0", community),   # sysName
        snmp_walk(ip, "1.3.6.1.2.1.47.1.1.1.1.11", community),  # 序列号表(entPhysicalSerialNum)
    )

    if not sys_descr and not sys_name:
        return {"ip": ip, "reachable": False}

    # 解析厂商/型号/类型
    vendor = None
    model = None
    device_type = None
    desc_lower = (sys_descr or "").lower()

    if "huawei" in desc_lower or "vrp" in desc_lower:
        vendor = "华为"
        m = re.search(r"(S\d+[-\w]+|AR\d+[-\w]+|NE\d+[-\w]+|CE\d+[-\w]+)", sys_descr or "")
        if m:
            model = m.group(1)
        device_type = "router" if ("router" in desc_lower or "ar" in (model or "").lower()) else "switch"
    elif "h3c" in desc_lower or "comware" in desc_lower:
        vendor = "H3C"
        model = _parse_h3c_model(sys_descr)
        device_type = _h3c_device_type(model)
    elif "cisco" in desc_lower:
        vendor = "Cisco"
        device_type = "router" if "router" in desc_lower else "switch"
    elif "ruijie" in desc_lower or "rg-" in desc_lower:
        vendor = "锐捷"
        device_type = "switch"
    elif "sangfor" in desc_lower:
        vendor = "深信服"
        device_type = "firewall"
    elif "linux" in desc_lower:
        vendor = "Linux"
        device_type = "server"

    # 序列号：取 entPhysicalSerialNum 表中第一个非空值
    serial = next((v for _, v in (serial_rows or []) if v), None)

    return {
        "ip": ip,
        "reachable": True,
        "name": sys_name or None,
        "vendor": vendor,
        "model": model,
        "device_type": device_type,
        "serial_number": serial,
        "sys_descr": sys_descr,
    }


# 实体 MIB 列 -> 组件字段（entPhysicalTable = 1.3.6.1.2.1.47.1.1.1.1）
ENTITY_COMPONENT_COLUMNS = {
    2: "descr",
    7: "name",
    8: "hardware_rev",
    9: "firmware_rev",
    10: "software_rev",
    11: "serial_number",
    12: "mfg_name",
    13: "model_name",
}


async def collect_entity_components(ip: str, community: str = "aiops", timeout: int = 8) -> list[dict]:
    """采集设备实体 MIB 组件明细（板卡/电源/风扇/传感器等）。

    返回按 entPhysicalIndex 归类的列表：
    [{phys_index, name, descr, model_name, serial_number, hardware_rev,
      firmware_rev, software_rev, mfg_name}, ...]

    10 列实体表**并发** walk（原串行单列最长 timeout*3 秒，慢设备会拖到
    几十秒超时；并发后整体耗时 ≈ 最慢一列）。
    """
    col_items = list(ENTITY_COMPONENT_COLUMNS.items())
    rows_list = await asyncio.gather(*[
        snmp_walk(ip, f"1.3.6.1.2.1.47.1.1.1.1.{col}", community, timeout=timeout)
        for col, _ in col_items
    ])
    by_index: dict[int, dict] = {}
    for (col, key), rows in zip(col_items, rows_list):
        for oid, val in rows:
            idx_str = oid.rsplit(".", 1)[-1]
            try:
                idx = int(idx_str)
            except ValueError:
                continue
            if val:
                by_index.setdefault(idx, {})[key] = val
    return [{"phys_index": idx, **data} for idx, data in sorted(by_index.items())]


def pick_chassis_serial(components: list[dict]) -> str | None:
    """机箱序列号：优先 phys_index=2（H3C 机箱），否则取第一个非空序列号。"""
    if not components:
        return None
    for c in components:
        if c.get("phys_index") == 2 and c.get("serial_number"):
            return c["serial_number"]
    for c in components:
        if c.get("serial_number"):
            return c["serial_number"]
    return None
