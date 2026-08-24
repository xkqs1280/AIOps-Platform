from app.services.syslog_service import _determine_severity, _extract_kv, _safe_int


def test_extract_key_value_fields_with_quoted_value():
    result = _extract_kv('srcip=10.0.0.1 action="blocked by policy" dstport=443')
    assert result["srcip"] == "10.0.0.1"
    assert result["action"] == "blocked by policy"
    assert result["dstport"] == "443"


def test_safe_int_and_severity_defaults():
    assert _safe_int("42") == 42
    assert _safe_int("not-a-number") is None
    assert _determine_severity({"severity": "critical"}) == "critical"
