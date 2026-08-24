#!/usr/bin/env python3
"""
SNMP Trap Handler - 由 snmptrapd 的 traphandle 调用

支持两种输入格式：

1. 无 MIB 文件（数字 OID 格式）:
   ubantu
   UDP: [192.168.11.100]:39427->[192.168.11.100]:162
   iso.3.6.1.2.1.1.3.0 0:2:37:38.20
   iso.3.6.1.6.3.1.1.4.1.0 iso.3.6.1.6.3.1.1.5.3
   iso.3.6.1.2.1.2.2.1.1.1 1
   iso.3.6.1.2.1.2.2.1.2.1 "GigabitEthernet0/0/1"
   iso.3.6.1.2.1.2.2.1.8.1 2

2. 有 MIB 文件（MIB 名称格式）:
   SW1
   UDP: [192.168.11.111]:12345->[192.168.11.100]:162
   DISMAN-EVENT-MIB::sysUpTimeInstance = Timeticks: (12345) 0:02:03.45
   SNMPv2-MIB::snmpTrapOID.0 = OID: IF-MIB::linkDown
   IF-MIB::ifIndex = INTEGER: 1
   IF-MIB::ifDescr = STRING: GigabitEthernet0/0/1
"""
import sys
import json
import re
import urllib.request

BACKEND_URL = "http://127.0.0.1:8000/api/v1/traps"
TIMEOUT = 5

# 关键 OID 常量
TRAP_OID_NUMERIC = "1.3.6.1.6.3.1.1.4.1"
SYSUPTIME_NUMERIC = "1.3.6.1.2.1.1.3"
IFDESCR_NUMERIC = "1.3.6.1.2.1.2.2.1.2"
IFINDEX_NUMERIC = "1.3.6.1.2.1.2.2.1.1"
IFNAME_NUMERIC = "1.3.6.1.2.1.31.1.1.1.1"
ENTPHYSICALNAME_NUMERIC = "1.3.6.1.2.1.47.1.1.1.1.7"


def extract_ip(text):
    """从文本中提取 IPv4 地址"""
    m = re.search(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', text)
    return m.group(1) if m else None


def normalize_oid(oid_str):
    """统一 OID 格式：iso. 前缀替换为 1.（iso 是 OID 根 1 的名称）"""
    oid = oid_str.strip()
    if oid.startswith("iso."):
        oid = "1." + oid[4:]
    oid = oid.lstrip(".")
    return oid


def parse_value(val):
    """解析 varbind 值，去除类型前缀和引号"""
    val = val.strip()
    # Remove surrounding quotes
    if val.startswith('"') and val.endswith('"'):
        return val[1:-1]
    if val.startswith("'") and val.endswith("'"):
        return val[1:-1]
    # Remove type prefixes: INTEGER:, STRING:, OID:, Timeticks:, Gauge32:, Counter32:, Hex-STRING:, IpAddress:
    for prefix in ["INTEGER:", "STRING:", "OID:", "Timeticks:", "Gauge32:", "Counter32:", "Hex-STRING:", "IpAddress:", "BITS:"]:
        if val.startswith(prefix):
            val = val[len(prefix):].strip()
            # Remove parentheses content like "(12345)"
            if val.startswith("("):
                close = val.find(")")
                if close > 0:
                    val = val[close + 1:].strip()
            # Remove quotes again after type stripping
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            elif val.startswith("'") and val.endswith("'"):
                val = val[1:-1]
            break
    return val


def is_varbind_line(line):
    """判断是否为 varbind 行（包含 = 或以 OID 开头）"""
    if "=" in line:
        return True
    # 数字 OID 格式：iso.x.x.x value 或 .x.x.x value
    if line.startswith("iso.") or line.startswith(".1."):
        return True
    # MIB 名称格式：MODULE::name value
    if "::" in line.split(" ")[0]:
        return True
    return False


def parse_trap(lines):
    """解析 snmptrapd traphandle 的 stdin 输入"""
    trap = {
        "source_ip": None,
        "trap_oid": None,
        "uptime": None,
        "varbinds": {}
    }

    for i, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue

        # Line 1: hostname (skip - usually just a hostname or IP)
        if i == 0 and not is_varbind_line(line) and "UDP:" not in line:
            continue

        # Line 2: UDP address info
        if "UDP:" in line:
            ips = re.findall(r'\[(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\]', line)
            if ips:
                trap["source_ip"] = ips[0]  # First [IP] is the source
            continue

        # Try "=" format first (MIB name mode)
        if "=" in line:
            parts = line.split("=", 1)
            key = parts[0].strip()
            value = parts[1].strip()
            key_norm = normalize_oid(key)

            # snmpTrapOID
            if "snmpTrapOID" in key or key_norm.startswith(TRAP_OID_NUMERIC):
                if "= OID:" in line:
                    raw_oid = line.split("= OID:", 1)[1].strip()
                else:
                    raw_oid = value
                # Remove "OID:" prefix if present
                if raw_oid.startswith("OID:"):
                    raw_oid = raw_oid[4:].strip()
                trap["trap_oid"] = normalize_oid(raw_oid)
                continue

            # sysUpTime
            if "sysUpTime" in key.lower() or key_norm.startswith(SYSUPTIME_NUMERIC):
                trap["uptime"] = parse_value(value)
                continue

            # Regular varbind
            trap["varbinds"][key] = parse_value(value)
            continue

        # Space-separated format (no MIB files)
        # Format: "OID value" (may have multiple spaces)
        space_idx = line.find(" ")
        if space_idx > 0:
            key = line[:space_idx].strip()
            value = line[space_idx + 1:].strip()
            key_norm = normalize_oid(key)

            if key_norm.startswith(TRAP_OID_NUMERIC):
                trap["trap_oid"] = normalize_oid(value)
            elif key_norm.startswith(SYSUPTIME_NUMERIC):
                trap["uptime"] = parse_value(value)
            else:
                trap["varbinds"][key_norm] = parse_value(value)

    return trap


def _log_path() -> str:
    """跨平台日志路径（Windows 用 %TEMP%，Linux 用 /tmp）。"""
    import os, tempfile
    return os.path.join(tempfile.gettempdir(), "trap_handler.log")


def main():
    data = sys.stdin.read()
    lines = data.strip().split("\n")

    # Debug log
    with open(_log_path(), "a") as f:
        f.write(f"=== {__import__('datetime').datetime.now()} ===\n")
        f.write(data)
        f.write("\n---\n")

    trap = parse_trap(lines)

    # Fallback: extract IP from any line
    if not trap["source_ip"]:
        for line in lines:
            if "UDP:" in line:
                ips = re.findall(r'\[(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\]', line)
                if ips:
                    trap["source_ip"] = ips[0]
                    break
            ip = extract_ip(line)
            if ip:
                trap["source_ip"] = ip
                break

    if not trap["source_ip"]:
        sys.stderr.write("No source IP found in trap\n")
        with open(_log_path(), "a") as f:
            f.write("SKIP: No source IP\n\n")
        return

    if not trap["trap_oid"]:
        sys.stderr.write(f"No trap OID found (source={trap['source_ip']})\n")
        with open(_log_path(), "a") as f:
            f.write(f"SKIP: source_ip={trap['source_ip']} trap_oid=None\n")
            f.write(f"Lines: {lines}\n\n")
        return

    # Log parsed result
    with open(_log_path(), "a") as f:
        f.write(f"PARSED: source_ip={trap['source_ip']} trap_oid={trap['trap_oid']}\n")
        f.write(f"Varbinds: {json.dumps(trap['varbinds'])}\n\n")

    # POST to backend API
    payload = json.dumps(trap).encode("utf-8")
    req = urllib.request.Request(
        BACKEND_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
        body = resp.read().decode("utf-8", errors="replace")
        sys.stderr.write(f"Trap posted: {trap['source_ip']} {trap['trap_oid']} -> {resp.status} {body}\n")
    except Exception as e:
        sys.stderr.write(f"Failed to post trap: {e}\n")
        with open(_log_path(), "a") as f:
            f.write(f"API ERROR: {e}\n\n")


if __name__ == "__main__":
    main()
