"""Syslog parsing and normalization engine for AIOps platform.

Receives raw syslog messages, identifies the vendor, parses using
vendor-specific rules, and normalizes into SecurityEvent records.
"""
import random
import re
from datetime import datetime, timezone

from sqlalchemy import select

from app.models.p3_security import SecurityEvent
from app.models.device import Device
from app.database import get_session


# ---------------------------------------------------------------------------
# Vendor identification patterns
# ---------------------------------------------------------------------------

_VENDOR_PATTERNS = [
    ("sangfor", re.compile(r"sangfor|NGAF|device\s*=", re.IGNORECASE)),
    ("hillstone", re.compile(r"Hillstone|StoneOS", re.IGNORECASE)),
    ("topsec", re.compile(r"TopSec|TopSecOS", re.IGNORECASE)),
    ("huawei", re.compile(r"Huawei|VRP", re.IGNORECASE)),
    ("h3c", re.compile(r"H3C|Comware", re.IGNORECASE)),
]

# Key=value extractor (supports both quoted and unquoted values)
_KV_RE = re.compile(r'(\w+)=(?:"([^"]*)"|(\S+))')

# IPv4 address pattern
_IPV4_RE = re.compile(r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b')

# Port pattern
_PORT_RE = re.compile(
    r'\b(?:port|srcport|sport|dstport|dport)[=\s:]+(\d{1,5})\b',
    re.IGNORECASE,
)

# Syslog PRI + timestamp prefix  e.g. <134>Aug 31 10:00:00
_SYSLOG_PREFIX_RE = re.compile(
    r'^<(?P<pri>\d+)>(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+'
)

# Severity keyword mapping (checked in priority order)
_SEVERITY_KEYWORDS = [
    ("critical", re.compile(r"attack|breach|compromise", re.IGNORECASE)),
    ("high", re.compile(r"intrusion|exploit|malware|virus|trojan", re.IGNORECASE)),
    ("medium", re.compile(r"scan|probe|anomaly|abnormal", re.IGNORECASE)),
    ("low", re.compile(r"info|notice", re.IGNORECASE)),
]

# Action normalization mapping
_ACTION_MAP = {
    "block": "blocked",
    "blocked": "blocked",
    "deny": "blocked",
    "denied": "blocked",
    "drop": "dropped",
    "dropped": "dropped",
    "allow": "allowed",
    "allowed": "allowed",
    "permit": "allowed",
    "detect": "detected",
    "detected": "detected",
    "alert": "detected",
    "timeout": "detected",
    "pass": "allowed",
}

# Category mapping for Sangfor type field
_SANGFOR_CATEGORY_MAP = {
    "attack": "intrusion",
    "virus": "malware",
    "scan": "anomaly",
    "ddos": "ddos",
    "abnormal": "anomaly",
}


# ---------------------------------------------------------------------------
# 1. Vendor identification
# ---------------------------------------------------------------------------

async def identify_vendor(session, raw_log, source_ip=None):
    """Identify the device vendor from log content and optionally the device table.

    Args:
        session: SQLAlchemy async session.
        raw_log: Raw syslog message string.
        source_ip: Optional IP of the sending device.

    Returns:
        Vendor string (sangfor/hillstone/topsec/huawei/h3c) or ``"unknown"``.
    """
    # 1) Content-based detection
    for vendor, pattern in _VENDOR_PATTERNS:
        if pattern.search(raw_log):
            return vendor

    # 2) Device-table lookup by IP
    if source_ip and session is not None:
        try:
            result = await session.execute(
                select(Device).where(Device.ip == source_ip)
            )
            device = result.scalar_one_or_none()
            if device and device.vendor:
                return device.vendor.lower()
        except Exception:
            pass

    return "unknown"


# ---------------------------------------------------------------------------
# 2. Main parse entry point
# ---------------------------------------------------------------------------

async def parse_syslog(session, raw_log, source_ip=None):
    """Identify the vendor and route to the appropriate parser.

    Returns a normalized event dict matching the SecurityEvent schema.
    """
    vendor = await identify_vendor(session, raw_log, source_ip)

    _PARSERS = {
        "sangfor": _parse_sangfor,
        "hillstone": _parse_hillstone,
        "topsec": _parse_topsec,
        "h3c": _parse_h3c,
        "huawei": _parse_huawei,
        "unknown": _parse_unknown,
    }

    parser = _PARSERS.get(vendor, _parse_unknown)
    parsed = parser(raw_log)
    parsed["vendor"] = vendor
    parsed.setdefault("raw_log", raw_log)
    return parsed


# ---------------------------------------------------------------------------
# 3. Vendor-specific parsers
# ---------------------------------------------------------------------------

def _extract_kv(text):
    """Extract key=value pairs from *text* (supports quoted and unquoted)."""
    pairs = {}
    for match in _KV_RE.finditer(text):
        key = match.group(1).lower()
        value = match.group(2) if match.group(2) is not None else match.group(3)
        pairs[key] = value
    return pairs


def _parse_sangfor(raw_log):
    """Parse Sangfor NGAF key=value format.

    Example::
        device="NGAF" type="attack" src="10.1.1.1" dst="192.168.1.1" action="block"
    """
    kv = _extract_kv(raw_log)

    raw_type = kv.get("type", "").lower()
    event_category = _SANGFOR_CATEGORY_MAP.get(raw_type, "anomaly")

    description = (
        kv.get("desc")
        or kv.get("description")
        or kv.get("msg")
        or kv.get("message")
        or f"Sangfor {raw_type or 'event'}"
    )

    return {
        "timestamp": datetime.now(timezone.utc),
        "event_category": event_category,
        "event_subcategory": raw_type or "event",
        "action": _ACTION_MAP.get(kv.get("action", "").lower(), "detected"),
        "description": description,
        "src_ip": kv.get("src"),
        "dst_ip": kv.get("dst"),
        "src_port": _safe_int(kv.get("sport") or kv.get("srcport")),
        "dst_port": _safe_int(kv.get("dport") or kv.get("dstport")),
        "protocol": kv.get("protocol", "").lower() or None,
        "threat_type": kv.get("threat") or kv.get("threattype"),
        "signature_id": kv.get("sig") or kv.get("signature"),
    }


def _parse_hillstone(raw_log):
    """Parse Hillstone StoneOS syslog format.

    Example::
        <134>Aug 31 10:00:00 Hillstone: session[12345]: policy=trust-untrust
        src=10.1.1.1 dst=192.168.1.1 action=deny dport=22 protocol=tcp
    """
    kv = _extract_kv(raw_log)
    pri_match = _SYSLOG_PREFIX_RE.match(raw_log)

    timestamp = datetime.now(timezone.utc)
    if pri_match:
        ts_str = pri_match.group("ts")
        try:
            parsed_ts = datetime.strptime(ts_str, "%b %d %H:%M:%S")
            parsed_ts = parsed_ts.replace(year=datetime.now().year, tzinfo=timezone.utc)
            timestamp = parsed_ts
        except ValueError:
            pass

    policy = kv.get("policy", "")
    action_raw = kv.get("action", "").lower()

    # Determine event category from content
    desc_lower = raw_log.lower()
    if "spoof" in desc_lower:
        event_category = "intrusion"
        event_subcategory = "ip_spoofing"
    elif "timeout" in desc_lower:
        event_category = "policy"
        event_subcategory = "session_timeout"
    elif "deny" in desc_lower or action_raw in ("deny", "drop"):
        event_category = "policy"
        event_subcategory = "policy_deny"
    else:
        event_category = "anomaly"
        event_subcategory = "session"

    # Build description
    session_id = re.search(r"session\[(\d+)\]", raw_log)
    desc_parts = []
    if session_id:
        desc_parts.append(f"session[{session_id.group(1)}]")
    if policy:
        desc_parts.append(f"policy={policy}")
    if not desc_parts:
        desc_parts.append("Hillstone event")
    description = " ".join(desc_parts)

    return {
        "timestamp": timestamp,
        "event_category": event_category,
        "event_subcategory": event_subcategory,
        "action": _ACTION_MAP.get(action_raw, "detected"),
        "description": description,
        "src_ip": kv.get("src"),
        "dst_ip": kv.get("dst"),
        "src_port": _safe_int(kv.get("sport") or kv.get("srcport")),
        "dst_port": _safe_int(kv.get("dport") or kv.get("dstport")),
        "protocol": kv.get("protocol", "").lower() or None,
        "threat_type": kv.get("threat"),
        "signature_id": session_id.group(1) if session_id else None,
    }


def _parse_topsec(raw_log):
    """Parse TopSec threat log format.

    Example::
        TopSecOS: threat_log: time=2024-01-01 10:00:00 src=10.1.1.1
        dst=192.168.1.1 type=attack action=block
    """
    # Extract everything after "threat_log:"
    threat_match = re.search(r"threat_log:\s*(.*)", raw_log, re.IGNORECASE)
    kv_text = threat_match.group(1) if threat_match else raw_log
    kv = _extract_kv(kv_text)

    # Also extract the time= field which may have a space (not captured by _KV_RE)
    time_match = re.search(r"time=(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", kv_text)
    timestamp = datetime.now(timezone.utc)
    if time_match:
        try:
            timestamp = datetime.strptime(
                time_match.group(1), "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    raw_type = kv.get("type", "").lower()
    if raw_type in ("virus", "malware"):
        event_category = "malware"
    elif raw_type in ("attack", "intrusion"):
        event_category = "intrusion"
    elif raw_type in ("abnormal", "anomaly"):
        event_category = "anomaly"
    elif raw_type in ("ddos",):
        event_category = "ddos"
    else:
        event_category = "anomaly"

    description = (
        kv.get("desc")
        or kv.get("description")
        or kv.get("msg")
        or f"TopSec {raw_type or 'threat'}"
    )

    return {
        "timestamp": timestamp,
        "event_category": event_category,
        "event_subcategory": raw_type or "threat",
        "action": _ACTION_MAP.get(kv.get("action", "").lower(), "detected"),
        "description": description,
        "src_ip": kv.get("src"),
        "dst_ip": kv.get("dst"),
        "src_port": _safe_int(kv.get("sport") or kv.get("srcport")),
        "dst_port": _safe_int(kv.get("dport") or kv.get("dstport")),
        "protocol": kv.get("protocol", "").lower() or None,
        "threat_type": kv.get("threat") or raw_type,
        "signature_id": kv.get("sig") or kv.get("id"),
    }


def _parse_h3c(raw_log):
    """Parse H3C / Comware syslog format.

    Example::
        <134>Jan 1 10:00:00 H3C %%01SEC/4/PACKET_FILTER:
        SrcIP=10.1.1.1 DstIP=192.168.1.1 action=block
    """
    kv = _extract_kv(raw_log)
    pri_match = _SYSLOG_PREFIX_RE.match(raw_log)

    timestamp = datetime.now(timezone.utc)
    if pri_match:
        ts_str = pri_match.group("ts")
        try:
            parsed_ts = datetime.strptime(ts_str, "%b %d %H:%M:%S")
            parsed_ts = parsed_ts.replace(year=datetime.now().year, tzinfo=timezone.utc)
            timestamp = parsed_ts
        except ValueError:
            pass

    # Extract H3C mnemonic / module info: %%ddMODULE/SEVERITY/MNEMONIC
    h3c_info = re.search(r"%%\d*(\w+)/(\d+)/(\w+)", raw_log)
    module = h3c_info.group(1) if h3c_info else None
    mnemonic = h3c_info.group(3) if h3c_info else None

    # SrcIP / DstIP (H3C uses capitalized keys)
    src_ip = kv.get("srcip") or kv.get("src") or kv.get("src-ip")
    dst_ip = kv.get("dstip") or kv.get("dst") or kv.get("dst-ip")

    desc_lower = raw_log.lower()
    if "attack" in desc_lower or "intrusion" in desc_lower:
        event_category = "intrusion"
    elif "virus" in desc_lower or "malware" in desc_lower:
        event_category = "malware"
    elif "policy" in desc_lower or "filter" in desc_lower:
        event_category = "policy"
    else:
        event_category = "audit"

    description = f"H3C {module or 'SYS'}: {mnemonic or 'event'}"

    return {
        "timestamp": timestamp,
        "event_category": event_category,
        "event_subcategory": mnemonic or module or "event",
        "action": _ACTION_MAP.get(kv.get("action", "").lower(), "detected"),
        "description": description,
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "src_port": _safe_int(kv.get("sport") or kv.get("srcport")),
        "dst_port": _safe_int(kv.get("dport") or kv.get("dstport")),
        "protocol": kv.get("protocol", "").lower() or None,
        "threat_type": mnemonic,
    }


def _parse_huawei(raw_log):
    """Parse Huawei VRP syslog format.

    Example::
        <189>2024-01-01 10:00:00 Huawei %%01SEC/4/ATTACK:
        SrcIP=10.1.1.1 DstIP=192.168.1.1 action=block
    """
    kv = _extract_kv(raw_log)

    timestamp = datetime.now(timezone.utc)

    # VRP uses full date format
    vrp_ts = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", raw_log)
    if vrp_ts:
        try:
            timestamp = datetime.strptime(
                vrp_ts.group(1), "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    # Extract VRP info: %%ddMODULE/SEVERITY/MNEMONIC
    vrp_info = re.search(r"%%\d*(\w+)/(\d+)/(\w+)", raw_log)
    module = vrp_info.group(1) if vrp_info else None
    mnemonic = vrp_info.group(3) if vrp_info else None

    src_ip = kv.get("srcip") or kv.get("src") or kv.get("src-ip")
    dst_ip = kv.get("dstip") or kv.get("dst") or kv.get("dst-ip")

    desc_lower = raw_log.lower()
    if "attack" in desc_lower or "intrusion" in desc_lower:
        event_category = "intrusion"
    elif "virus" in desc_lower or "malware" in desc_lower:
        event_category = "malware"
    elif "policy" in desc_lower or "filter" in desc_lower:
        event_category = "policy"
    else:
        event_category = "audit"

    description = f"Huawei {module or 'VRP'}: {mnemonic or 'event'}"

    return {
        "timestamp": timestamp,
        "event_category": event_category,
        "event_subcategory": mnemonic or module or "event",
        "action": _ACTION_MAP.get(kv.get("action", "").lower(), "detected"),
        "description": description,
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "src_port": _safe_int(kv.get("sport") or kv.get("srcport")),
        "dst_port": _safe_int(kv.get("dport") or kv.get("dstport")),
        "protocol": kv.get("protocol", "").lower() or None,
        "threat_type": mnemonic,
    }


def _parse_unknown(raw_log):
    """Best-effort extraction of IPs, ports, and keywords from unknown formats."""
    # Find all IPv4 addresses
    ips = _IPV4_RE.findall(raw_log)
    src_ip = ips[0] if len(ips) >= 1 else None
    dst_ip = ips[1] if len(ips) >= 2 else None

    # Find ports
    ports = _PORT_RE.findall(raw_log)

    # Try key=value extraction as fallback
    kv = _extract_kv(raw_log)
    if not src_ip:
        src_ip = kv.get("src") or kv.get("srcip")
    if not dst_ip:
        dst_ip = kv.get("dst") or kv.get("dstip")

    desc_lower = raw_log.lower()
    if "attack" in desc_lower or "breach" in desc_lower:
        event_category = "intrusion"
    elif "virus" in desc_lower or "malware" in desc_lower:
        event_category = "malware"
    elif "ddos" in desc_lower or "flood" in desc_lower:
        event_category = "ddos"
    elif "scan" in desc_lower or "probe" in desc_lower:
        event_category = "anomaly"
    elif "policy" in desc_lower or "deny" in desc_lower:
        event_category = "policy"
    else:
        event_category = "audit"

    # Determine action from keywords
    if "block" in desc_lower or "deny" in desc_lower or "drop" in desc_lower:
        action = "blocked"
    elif "allow" in desc_lower or "permit" in desc_lower or "pass" in desc_lower:
        action = "allowed"
    elif "detect" in desc_lower or "alert" in desc_lower:
        action = "detected"
    else:
        action = "detected"

    return {
        "timestamp": datetime.now(timezone.utc),
        "event_category": event_category,
        "event_subcategory": "unknown",
        "action": action,
        "description": raw_log[:512],
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "src_port": _safe_int(ports[0]) if ports else _safe_int(kv.get("sport")),
        "dst_port": _safe_int(ports[1]) if len(ports) >= 2 else _safe_int(kv.get("dport")),
        "protocol": kv.get("protocol", "").lower() or None,
        "threat_type": kv.get("type"),
    }


# ---------------------------------------------------------------------------
# 4. Normalization
# ---------------------------------------------------------------------------

def _determine_severity(parsed_event):
    """Determine severity from event keywords and category."""
    # Check if severity was explicitly set by the parser
    explicit = parsed_event.get("severity")
    if explicit:
        return explicit

    # Build a text blob to search for keywords
    text = " ".join(
        str(v)
        for v in (
            parsed_event.get("description", ""),
            parsed_event.get("event_category", ""),
            parsed_event.get("event_subcategory", ""),
            parsed_event.get("threat_type", ""),
        )
        if v
    )

    for level, pattern in _SEVERITY_KEYWORDS:
        if pattern.search(text):
            return level

    return "info"


def _safe_int(value):
    """Safely convert *value* to int, return None on failure."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


async def normalize_event(session, parsed_event, source_ip=None):
    """Map a parsed event dict to a SecurityEvent record and persist it.

    Args:
        session: SQLAlchemy async session.
        parsed_event: Dict produced by a vendor parser.
        source_ip: Optional source IP for device lookup.

    Returns:
        The saved SecurityEvent instance.
    """
    severity = _determine_severity(parsed_event)

    # Resolve device_id from source_ip if not already set
    device_id = parsed_event.get("device_id")
    if not device_id and source_ip and session is not None:
        try:
            result = await session.execute(
                select(Device).where(Device.ip == source_ip)
            )
            device = result.scalar_one_or_none()
            if device:
                device_id = device.id
        except Exception:
            pass

    event = SecurityEvent(
        device_id=device_id,
        timestamp=parsed_event.get("timestamp", datetime.now(timezone.utc)),
        event_category=parsed_event.get("event_category"),
        event_subcategory=parsed_event.get("event_subcategory"),
        severity=severity,
        action=parsed_event.get("action"),
        description=parsed_event.get("description"),
        src_ip=parsed_event.get("src_ip"),
        src_port=parsed_event.get("src_port"),
        dst_ip=parsed_event.get("dst_ip"),
        dst_port=parsed_event.get("dst_port"),
        protocol=parsed_event.get("protocol"),
        app=parsed_event.get("app"),
        threat_type=parsed_event.get("threat_type"),
        signature_id=parsed_event.get("signature_id"),
        cve=parsed_event.get("cve"),
        threat_score=parsed_event.get("threat_score"),
        ip_reputation=parsed_event.get("ip_reputation"),
        raw_log=parsed_event.get("raw_log"),
    )

    session.add(event)
    await session.flush()
    return event


# ---------------------------------------------------------------------------
# 5. Main ingestion pipeline
# ---------------------------------------------------------------------------

async def ingest_syslog(session, raw_log, source_ip=None):
    """Full ingestion pipeline: identify → parse → normalize → save.

    Args:
        session: SQLAlchemy async session.
        raw_log: Raw syslog message string.
        source_ip: Optional IP of the sending device.

    Returns:
        The created SecurityEvent record.
    """
    parsed = await parse_syslog(session, raw_log, source_ip)
    event = await normalize_event(session, parsed, source_ip)
    await session.commit()
    return event


# ---------------------------------------------------------------------------
# 6. Sample log generation
# ---------------------------------------------------------------------------

# Sample IP pools
_INTERNAL_IPS = [
    "10.10.10.5", "10.10.10.6", "10.10.10.7", "10.10.10.8",
    "10.10.20.3", "10.10.20.4", "10.1.1.1", "10.1.1.2", "10.1.1.3",
    "10.20.30.1", "10.20.30.2", "10.20.30.3", "172.16.1.5", "172.16.1.6",
]
_EXTERNAL_IPS = [
    "192.168.1.100", "192.168.1.101", "192.168.1.102", "192.168.2.1",
    "192.168.2.2", "192.168.2.3", "192.168.1.50", "192.168.1.51",
    "192.168.1.52", "192.168.3.10", "192.168.3.11",
]

_SAMPLE_SANGFOR_LOGS = [
    lambda s, d: f'device="NGAF" type="attack" src="{s}" dst="{d}" action="block" desc="SQL Injection detected" sport={random.randint(1024, 65535)} dport=80 protocol=tcp',
    lambda s, d: f'device="NGAF" type="attack" src="{s}" dst="{d}" action="block" desc="XSS attack detected" sport={random.randint(1024, 65535)} dport=443 protocol=tcp',
    lambda s, d: f'device="NGAF" type="scan" src="{s}" dst="{d}" action="detect" desc="Port scan detected" sport={random.randint(1024, 65535)} dport=22 protocol=tcp',
    lambda s, d: f'device="NGAF" type="attack" src="{s}" dst="{d}" action="block" desc="DDoS flood detected" sport={random.randint(1024, 65535)} dport=53 protocol=udp',
    lambda s, d: f'device="NGAF" type="virus" src="{s}" dst="{d}" action="block" desc="Trojan.Generic.123 detected" sport={random.randint(1024, 65535)} dport=8080 protocol=tcp',
]

_SAMPLE_HILLSTONE_LOGS = [
    lambda s, d: f'<134>Aug 31 10:{random.randint(0,59):02d}:{random.randint(0,59):02d} Hillstone: session[{random.randint(10000,99999)}]: policy=trust-untrust src={s} dst={d} action=deny dport=22 protocol=tcp',
    lambda s, d: f'<134>Aug 31 10:{random.randint(0,59):02d}:{random.randint(0,59):02d} Hillstone: session[{random.randint(10000,99999)}]: policy=trust-untrust src={s} dst={d} action=timeout',
    lambda s, d: f'<134>Aug 31 10:{random.randint(0,59):02d}:{random.randint(0,59):02d} Hillstone: ipspoof[{random.randint(10000,99999)}]: src={s} dst={d} action=drop desc="IP spoofing detected" protocol=tcp',
]

_SAMPLE_TOPSEC_LOGS = [
    lambda s, d: f'TopSecOS: threat_log: time=2024-01-31 10:{random.randint(0,59):02d}:{random.randint(0,59):02d} src={s} dst={d} type=attack action=block desc="Trojan.Win32.Generic detected" sport={random.randint(1024, 65535)} dport=443 protocol=tcp',
    lambda s, d: f'TopSecOS: threat_log: time=2024-01-31 10:{random.randint(0,59):02d}:{random.randint(0,59):02d} src={s} dst={d} type=virus action=block desc="Virus.Win32.Example.b" sport={random.randint(1024, 65535)} dport=80 protocol=tcp',
    lambda s, d: f'TopSecOS: threat_log: time=2024-01-31 10:{random.randint(0,59):02d}:{random.randint(0,59):02d} src={s} dst={d} type=abnormal action=detect desc="Abnormal traffic pattern detected" protocol=udp',
    lambda s, d: f'TopSecOS: threat_log: time=2024-01-31 10:{random.randint(0,59):02d}:{random.randint(0,59):02d} src={s} dst={d} type=attack action=block desc="Web attack: command injection" sport={random.randint(1024, 65535)} dport=8080 protocol=tcp',
]


async def generate_sample_logs(session, device_id=None, count=50):
    """Generate realistic sample syslog data for testing.

    Produces a mix of Sangfor, Hillstone, and TopSec events covering
    attacks, malware, scans, policy violations, and anomalies.

    Args:
        session: SQLAlchemy async session.
        device_id: Optional device ID to associate events with.
        count: Number of sample events to generate (default 50).

    Returns:
        Number of events successfully generated.
    """
    sample_pool = (
        _SAMPLE_SANGFOR_LOGS + _SAMPLE_HILLSTONE_LOGS + _SAMPLE_TOPSEC_LOGS
    )

    generated = 0
    for _ in range(count):
        src = random.choice(_INTERNAL_IPS)
        dst = random.choice(_EXTERNAL_IPS)
        template = random.choice(sample_pool)
        raw_log = template(src, dst)

        try:
            parsed = await parse_syslog(session, raw_log, source_ip=None)

            # Attach device_id if provided
            if device_id is not None:
                parsed["device_id"] = device_id

            await normalize_event(session, parsed, source_ip=None)
            generated += 1
        except Exception:
            # Skip individual failures
            continue

    await session.commit()
    return generated
