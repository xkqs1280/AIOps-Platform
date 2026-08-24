# -*- coding: utf-8 -*-
"""
SNMP 探测工具（独立版，供生产环境测试使用）

功能：
  - 对多台设备批量测试 SNMP，GET / WALK 指定 OID 组，记录返回值和错误类型
  - 内置 OID 组：system(系统信息) / entity(实体MIB:序列号/型号/厂商) /
    lldp(基础LLDP) / lldp-med(LLDP-MED) / lldp-neighbor(LLDP邻居) / ifmib(接口)
  - 也支持 --oids 自定义 OID 列表
  - 结果同时打印到控制台并保存 CSV（便于后续分析）

依赖：pysnmp（纯 Python）。首次运行会自动提示安装命令。

用法示例：
  # 单台，默认组 system,entity
  python snmp_probe.py --hosts 192.168.1.10 --community aiops

  # 多台 + 多组 + 保存结果
  python snmp_probe.py --hosts 192.168.1.10,192.168.1.11 --community aiops --groups system,entity,lldp,lldp-med,lldp-neighbor --out result.csv

  # 从文件读设备列表（每行一个 IP 或 IP:port）
  python snmp_probe.py --hosts-file hosts.txt --community public --version v2c

  # 自定义 OID
  python snmp_probe.py --hosts 192.168.1.10 --community public --oids 1.3.6.1.2.1.1.1.0,1.3.6.1.2.1.1.5.0
"""
import argparse
import asyncio
import csv
import datetime
import os
import sys

# ---------- 依赖检查 ----------
try:
    from pysnmp.hlapi.asyncio import (
        SnmpEngine, CommunityData, ContextData, ObjectType, ObjectIdentity,
        UdpTransportTarget, getCmd, walkCmd,
    )
    from pysnmp.proto.rfc1905 import NoSuchObject, NoSuchInstance
except ImportError:
    print("未安装 pysnmp，请先执行：\n    pip install pysnmp\n")
    sys.exit(1)

# ---------- 内置 OID 组 ----------
# GET 型：名称 -> OID
GET_GROUPS = {
    "system": {
        "sysDescr":          "1.3.6.1.2.1.1.1.0",
        "sysObjectID":       "1.3.6.1.2.1.1.2.0",
        "sysUpTime":         "1.3.6.1.2.1.1.3.0",
        "sysContact":        "1.3.6.1.2.1.1.4.0",
        "sysName":           "1.3.6.1.2.1.1.5.0",
        "sysLocation":       "1.3.6.1.2.1.1.6.0",
    },
    "ifmib": {
        "ifNumber":          "1.3.6.1.2.1.2.1.0",
    },
    "lldp": {
        "lldpMessageTxInterval":    "1.0.8802.1.1.2.1.1.1.1.0",
        "lldpLocChassisIdSubtype":  "1.0.8802.1.1.2.1.1.1.3.1",
        "lldpLocChassisId":         "1.0.8802.1.1.2.1.1.1.3.2",
        "lldpLocSysName":           "1.0.8802.1.1.2.1.1.1.3.3",
        "lldpLocSysDesc":           "1.0.8802.1.1.2.1.1.1.3.4",
    },
    "lldp-med": {
        "lldpXMedLocDeviceClass":       "1.0.8802.1.1.2.1.5.4795.1.1.1",
        "lldpXMedFastStartRepeatCount": "1.0.8802.1.1.2.1.5.4795.1.1.3",
        "lldpXMedLocHardwareRev":       "1.0.8802.1.1.2.1.5.4795.1.2.2",
        "lldpXMedLocFirmwareRev":       "1.0.8802.1.1.2.1.5.4795.1.2.3",
        "lldpXMedLocSoftwareRev":       "1.0.8802.1.1.2.1.5.4795.1.2.4",
        "lldpXMedLocSerialNum":         "1.0.8802.1.1.2.1.5.4795.1.2.5",
        "lldpXMedLocMfgName":           "1.0.8802.1.1.2.1.5.4795.1.2.6",
        "lldpXMedLocModelName":         "1.0.8802.1.1.2.1.5.4795.1.2.7",
        "lldpXMedLocAssetID":           "1.0.8802.1.1.2.1.5.4795.1.2.8",
    },
    "serial": {
        "entPhysicalSerialNum.1":  "1.3.6.1.2.1.47.1.1.1.1.11.1",
        "entPhysicalSerialNum.2":  "1.3.6.1.2.1.47.1.1.1.1.11.2",
        "entPhysicalSerialNum.3":  "1.3.6.1.2.1.47.1.1.1.1.11.3",
        "hh3cManuSerialNum.1":     "1.3.6.1.4.1.25506.2.6.1.2.1.1.2.1",
        "hh3cManuSerialNum.2":     "1.3.6.1.4.1.25506.2.6.1.2.1.1.2.2",
        "hh3cManuSerialNum.3":     "1.3.6.1.4.1.25506.2.6.1.2.1.1.2.3",
        "hh3cManuBOM.2":           "1.3.6.1.4.1.25506.2.6.1.2.1.1.4.2",
    },
}

# WALK 型：名称 -> (起始 OID, 说明)
WALK_GROUPS = {
    "entity": {
        "entPhysicalDescr":       "1.3.6.1.2.1.47.1.1.1.1.2",
        "entPhysicalName":        "1.3.6.1.2.1.47.1.1.1.1.7",
        "entPhysicalHardwareRev": "1.3.6.1.2.1.47.1.1.1.1.8",
        "entPhysicalFirmwareRev": "1.3.6.1.2.1.47.1.1.1.1.9",
        "entPhysicalSoftwareRev": "1.3.6.1.2.1.47.1.1.1.1.10",
        "entPhysicalSerialNum":   "1.3.6.1.2.1.47.1.1.1.1.11",
        "entPhysicalMfgName":     "1.3.6.1.2.1.47.1.1.1.1.12",
        "entPhysicalModelName":   "1.3.6.1.2.1.47.1.1.1.1.13",
    },
    "lldp-neighbor": {
        "lldpRemTable(标准)":   "1.0.8802.1.1.2.1.1.1.4",
        "lldpV2Rem(2009版)":    "1.0.8802.1.1.2.1.13.1.4",
    },
    "manu": {
        "hh3cEntityExtManu(制造信息)": "1.3.6.1.4.1.25506.2.6.1.2",
    },
    "lldp-full": {
        "lldpMIB 全部":         "1.0.8802.1.1.2.1.1",
    },
}

ALL_GROUPS = {**GET_GROUPS, **WALK_GROUPS}


# ---------- 工具函数 ----------
def value_to_str(val) -> str:
    """pysnmp 值对象 → 字符串；noSuchObject/Instance、二进制转 Hex 显示。"""
    if isinstance(val, (NoSuchObject, NoSuchInstance)):
        return "<不存在>"
    try:
        raw = val.asOctets()
        if any(b < 32 and b not in (9, 10, 13) or b >= 127 for b in raw):
            return " ".join(f"{b:02X}" for b in raw)
    except (AttributeError, TypeError):
        pass
    return str(val)


# ---------- 探测核心 ----------
async def snmp_get(ip, oid, community, port, version, timeout, retries):
    """GET 单个 OID，返回 (成功?, 值或错误描述)。"""
    mp = 1 if version == "v2c" else 0
    try:
        errorIndication, errorStatus, errorIndex, varBinds = await getCmd(
            SnmpEngine(),
            CommunityData(community, mpModel=mp),
            UdpTransportTarget((ip, port), timeout=timeout, retries=retries),
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
        )
        if errorIndication:
            return False, f"错误: {errorIndication}"
        if errorStatus:
            return False, f"错误: {errorStatus.prettyPrint()}"
        if varBinds and varBinds[0][1] is not None:
            val = varBinds[0][1]
            if isinstance(val, (NoSuchObject, NoSuchInstance)):
                return False, "无此对象(noSuchObject/noSuchInstance)"
            return True, value_to_str(val)
        return False, "无值"
    except Exception as e:
        return False, f"异常: {type(e).__name__}: {e}"


async def snmp_walk(ip, oid, community, port, version, timeout, retries, max_nodes=200):
    """WALK 一个子树，返回节点列表 [(oid, value)]；带总超时防无限阻塞。"""
    mp = 1 if version == "v2c" else 0
    rows = []

    async def _walk():
        async for errorIndication, errorStatus, errorIndex, varBinds in walkCmd(
            SnmpEngine(),
            CommunityData(community, mpModel=mp),
            UdpTransportTarget((ip, port), timeout=timeout, retries=retries),
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
            lexicographicMode=False,
        ):
            if errorIndication or errorStatus:
                break
            for oid_obj, val in varBinds:
                rows.append((oid_obj.getOid().prettyPrint().lstrip("."), value_to_str(val)))
            if len(rows) >= max_nodes:
                break

    walk_limit = timeout * (retries + 1) * 6 + 10
    try:
        await asyncio.wait_for(_walk(), timeout=walk_limit)
    except asyncio.TimeoutError:
        return [("error", f"WALK 超时({walk_limit}s)")]
    except Exception as e:
        return [("error", f"异常: {type(e).__name__}: {e}")]
    return rows


def parse_hosts(hosts_str, hosts_file):
    """解析设备列表，支持 IP、IP:port；分隔符：逗号 / 换行 / 分号 / 制表符。"""
    import re
    hosts = []
    if hosts_str:
        for h in re.split(r"[,;\t\n\r]+", hosts_str):
            h = h.strip()
            if h:
                hosts.append(h)
    if hosts_file:
        with open(hosts_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    hosts.append(line)
    if not hosts:
        print("错误：请提供 --hosts 或 --hosts-file")
        sys.exit(2)
    result = []
    for h in hosts:
        if ":" in h:
            ip, port = h.rsplit(":", 1)
            result.append((ip, int(port)))
        else:
            result.append((h, 161))
    return result


# ---------- 主流程 ----------
async def amain():
    parser = argparse.ArgumentParser(description="SNMP 探测工具：批量测试设备 SNMP 并记录返回值")
    parser.add_argument("--hosts", help="设备列表，逗号/换行/分号分隔，支持 IP:port")
    parser.add_argument("--hosts-file", help="设备列表文件，每行一个 IP 或 IP:port（# 为注释）")
    parser.add_argument("--community", default="aiops", help="SNMP community，逗号分隔依次尝试")
    parser.add_argument("--version", default="v2c", choices=["v1", "v2c"], help="SNMP 版本")
    parser.add_argument("--port", type=int, default=161, help="SNMP 端口")
    parser.add_argument("--groups", default="system,entity",
                        help=f"测试组，逗号分隔。可用: {', '.join(ALL_GROUPS)}")
    parser.add_argument("--oids", help="自定义 OID 列表（逗号分隔），与 --groups 互斥")
    parser.add_argument("--timeout", type=float, default=5, help="单次请求超时秒数")
    parser.add_argument("--retries", type=int, default=0, help="重试次数")
    parser.add_argument("--walk-max", type=int, default=200, help="每个 WALK 最多记录节点数")
    parser.add_argument("--out", default=None, help="CSV 输出文件（默认：snmp_probe_result_<时间戳>.csv）")
    args = parser.parse_args()

    hosts = parse_hosts(args.hosts, args.hosts_file)
    communities = [c.strip() for c in args.community.split(",") if c.strip()]

    # 输出文件：默认带时间戳，避免覆盖；始终打印绝对路径便于查找
    if not args.out:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        args.out = f"snmp_probe_result_{ts}.csv"
    out_abs = os.path.abspath(args.out)

    # 组装测试项
    tests = []  # (group, name, oid, is_walk)
    if args.oids:
        for i, oid in enumerate(args.oids.split(",")):
            oid = oid.strip()
            if oid:
                tests.append(("custom", f"custom_{i}", oid, False))
    else:
        for g in args.groups.split(","):
            g = g.strip()
            if g in GET_GROUPS:
                for name, oid in GET_GROUPS[g].items():
                    tests.append((g, name, oid, False))
            elif g in WALK_GROUPS:
                for name, oid in WALK_GROUPS[g].items():
                    tests.append((g, name, oid, True))
            elif g:
                print(f"警告：未知组 '{g}'，跳过")
    if not tests:
        print("错误：没有可测试的 OID，请检查 --groups")
        sys.exit(2)

    print(f"设备 {len(hosts)} 台 | community 尝试 {communities} | 测试项 {len(tests)} 个 | 输出 {out_abs}")
    print("=" * 78)

    results = []  # (host, community, group, name, oid, status, value)
    try:
        for ip, port in hosts:
            print(f"\n>>> 设备 {ip}:{port}")
            # 先探测一个基础 OID 确定可用 community
            active_comm = None
            for comm in communities:
                ok, val = await snmp_get(ip, "1.3.6.1.2.1.1.1.0", comm, port, args.version, args.timeout, args.retries)
                if ok:
                    active_comm = comm
                    print(f"  community='{comm}' 可用 -> sysDescr: {val[:60]}")
                    break
                print(f"  community='{comm}' 不可用 -> {val}")
            if active_comm is None:
                results.append((ip, ";".join(communities), "-", "sysDescr", "1.3.6.1.2.1.1.1.0", "FAIL", "所有 community 均不可达"))
                continue

            for group, name, oid, is_walk in tests:
                if is_walk:
                    rows = await snmp_walk(ip, oid, active_comm, port, args.version, args.timeout, args.retries, args.walk_max)
                    if not rows:
                        status, disp = "EMPTY", "无节点"
                    elif rows[0][0] == "error":
                        status, disp = "FAIL", rows[0][1]
                    else:
                        status = f"OK({len(rows)})"
                        disp = "；".join(f"{o}={v}" for o, v in rows[:6])
                        if len(rows) > 6:
                            disp += f" ...（共 {len(rows)} 个节点）"
                    print(f"  [WALK] {name:<20} {oid:<38} -> {status}: {disp[:90]}")
                    for o, v in rows:
                        results.append((ip, active_comm, group, name, o, status, v))
                else:
                    ok, val = await snmp_get(ip, oid, active_comm, port, args.version, args.timeout, args.retries)
                    status = "OK" if ok else "FAIL"
                    print(f"  [GET ] {name:<20} {oid:<38} -> {status}: {val[:90]}")
                    results.append((ip, active_comm, group, name, oid, status, val))
    except KeyboardInterrupt:
        print("\n用户中断，保存已采集的结果...")

    # 写 CSV（中断/异常也会保存已采集结果）
    with open(out_abs, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["host", "community", "group", "oid_name", "oid", "status", "value"])
        w.writerows(results)
    print("\n" + "=" * 78)
    print(f"完成，共 {len(results)} 条记录已保存到 {out_abs}")


def main():
    asyncio.run(amain())


if __name__ == "__main__":
    main()
