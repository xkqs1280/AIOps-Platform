"""配置基线核查引擎单测。

覆盖：规则集加载与错误处理、五类 match 语义（any/absent/count/block/block_absent）、
配置块边界判定、敏感值打码，以及两份真实风格夹具的端到端结论。
"""
from pathlib import Path

import pytest

from app.services import config_audit_engine as eng

FIXTURES = Path(__file__).resolve().parent / "fixtures"
UNHEALTHY = FIXTURES / "h3c_sw1_unhealthy.cfg"
HEALTHY = FIXTURES / "h3c_sw2_healthy.cfg"

ALL_IDS = {f"CFG-{i:03d}" for i in range(1, 15)}


def _doc(path):
    return eng.ConfigDocument(str(path), path.read_text(encoding="utf-8"))


def _findings(path):
    findings, passed = eng.audit_document(_doc(path))
    return {f["id"]: f for f in findings}, {p["id"] for p in passed}


# ---------------------------------------------------------------------------
# 规则集加载
# ---------------------------------------------------------------------------

def test_rules_load_and_shape():
    rs = eng.load_rules()
    assert len(rs["rules"]) == 14
    assert {r["id"] for r in rs["rules"]} == ALL_IDS
    for r in rs["rules"]:
        assert r["severity"] in ("high", "medium", "low")
        assert r["category"] in rs["categories"]
        assert r["advice"]


def test_rule_ids_do_not_collide_with_runtime_checks():
    """配置基线用 CFG-* 前缀，不能与现有运行态核查的 SEC-1.1 系列撞车。

    compliance_checks 表有 (device_id, control_id) 唯一约束，撞车会互相覆盖。
    """
    rs = eng.load_rules()
    assert all(r["id"].startswith("CFG-") for r in rs["rules"])
    # 长度必须放得进 control_id = String(16)
    assert all(len(r["id"]) <= 16 for r in rs["rules"])


def test_missing_rules_file_raises(tmp_path):
    with pytest.raises(eng.ConfigRuleError):
        eng.load_rules(str(tmp_path / "nope.json"))


def test_corrupt_rules_file_raises(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    with pytest.raises(eng.ConfigRuleError):
        eng.load_rules(str(bad))


# ---------------------------------------------------------------------------
# 端到端：两份夹具
# ---------------------------------------------------------------------------

def test_unhealthy_config_triggers_all_rules():
    """问题夹具应把 14 条规则全部打出来。"""
    findings, passed = _findings(UNHEALTHY)
    assert set(findings) == ALL_IDS
    assert passed == set()


def test_healthy_config_passes_every_rule():
    findings, passed = _findings(HEALTHY)
    assert findings == {}, f"误报：{sorted(findings)}"
    assert passed == ALL_IDS


def test_findings_sorted_by_severity():
    findings, _ = eng.audit_document(_doc(UNHEALTHY))
    sevs = [f["severity"] for f in findings]
    assert sevs == sorted(sevs, key=lambda s: {"high": 0, "medium": 1, "low": 2}[s])


# ---------------------------------------------------------------------------
# 敏感值打码
# ---------------------------------------------------------------------------

def test_plaintext_password_is_masked_in_evidence():
    """SEC-003 的命中行就是口令本身，证据必须打码后才能出报告。"""
    findings, _ = _findings(UNHEALTHY)
    hits = findings["CFG-003"]["hits"]
    assert hits
    for h in hits:
        assert "Admin@123" not in h["text"]
        assert "******" in h["text"]
        assert "password simple" in h["text"]  # 保留可读前缀，便于定位


def test_mask_can_be_disabled_for_local_debug():
    doc = _doc(UNHEALTHY)
    rule = next(r for r in eng.load_rules()["rules"] if r["id"] == "CFG-003")
    hits = eng.evaluate_rule(doc, rule, mask=False)
    assert any("Admin@123" in h["text"] for h in hits)


def test_mask_rule_declares_its_own_secret():
    """打码范围由规则自己声明，避免在引擎里硬编码敏感字段清单。"""
    rule = next(r for r in eng.load_rules()["rules"] if r["id"] == "CFG-003")
    assert rule.get("mask")
    # 除 CFG-003 外不应有规则被误加 mask（public 是默认值不是秘密，需保留可见）
    others = [r["id"] for r in eng.load_rules()["rules"] if r.get("mask") and r["id"] != "CFG-003"]
    assert others == []
    c2 = next(r for r in eng.load_rules()["rules"] if r["id"] == "CFG-002")
    assert not c2.get("mask")


# ---------------------------------------------------------------------------
# 各 match 类型语义
# ---------------------------------------------------------------------------

def test_absent_rule_reports_missing_marker():
    findings, _ = _findings(UNHEALTHY)
    hits = findings["CFG-004"]["hits"]
    assert len(hits) == 1
    assert hits[0]["no"] is None
    assert hits[0]["missing"] is True
    assert "未" in hits[0]["text"]


def test_present_rule_hit_not_flagged_missing():
    """普通命中项必须 missing=False，否则证据会被误写成「未检出」。"""
    findings, _ = _findings(UNHEALTHY)
    hits = findings["CFG-001"]["hits"]
    assert hits and all(h["missing"] is False for h in hits)


def test_any_rule_reports_line_numbers():
    findings, _ = _findings(UNHEALTHY)
    hits = findings["CFG-001"]["hits"]
    assert len(hits) == 1
    assert hits[0]["no"] > 0
    assert "telnet server enable" in hits[0]["text"]


def test_count_rule_dedupes_same_user_by_username():
    """同一账号的 class manage 与 privilege level 15 不能算两个人。"""
    cfg = """
local-user admin class manage
local-user admin privilege level 15
"""
    doc = eng.ConfigDocument("x", cfg)
    rule = next(r for r in eng.load_rules()["rules"] if r["id"] == "CFG-013")
    assert eng.evaluate_rule(doc, rule) == []

    doc2 = eng.ConfigDocument("x", cfg + "local-user ops privilege level 15\n")
    assert len(eng.evaluate_rule(doc2, rule)) == 2


def test_block_rule_matches_only_vlan1():
    rule = next(r for r in eng.load_rules()["rules"] if r["id"] == "CFG-008")
    # VLAN 100 上配管理地址是合规做法，不能误报
    doc = eng.ConfigDocument("x", "interface Vlan-interface100\n ip address 10.0.0.1 255.255.255.0\n")
    assert eng.evaluate_rule(doc, rule) == []
    # Vlan-interface10 也不能被 Vlan-interface1 的锚点吃掉
    doc2 = eng.ConfigDocument("x", "interface Vlan-interface10\n ip address 10.0.0.1 255.255.255.0\n")
    assert eng.evaluate_rule(doc2, rule) == []
    # 华为写法 Vlanif1 同样要认
    doc3 = eng.ConfigDocument("x", "interface Vlanif1\n  ip address 10.0.0.1 255.255.255.0\n")
    assert len(eng.evaluate_rule(doc3, rule)) == 2


def test_block_rule_stops_at_next_section():
    """VLAN1 块内没有地址、地址写在别的接口下 → 不算 VLAN1 问题。"""
    rule = next(r for r in eng.load_rules()["rules"] if r["id"] == "CFG-008")
    doc = eng.ConfigDocument("x", (
        "interface Vlan-interface1\n"
        " description mgmt\n"
        "#\n"
        "interface Vlan-interface200\n"
        " ip address 10.0.0.1 255.255.255.0\n"
    ))
    assert eng.evaluate_rule(doc, rule) == []


def test_block_absent_requires_scheme_within_vty():
    rule = next(r for r in eng.load_rules()["rules"] if r["id"] == "CFG-011")
    ok = eng.ConfigDocument("x", "user-interface vty 0 4\n authentication-mode scheme\n")
    assert eng.evaluate_rule(ok, rule) == []
    bad = eng.ConfigDocument("x", "user-interface vty 0 4\n authentication-mode password\n")
    assert len(eng.evaluate_rule(bad, rule)) == 1
    # 别的配置块里出现 authentication-mode 不能顶替 VTY 的缺失
    elsewhere = eng.ConfigDocument("x", (
        "user-interface vty 0 4\n authentication-mode password\n"
        "#\n"
        "domain system\n authentication-mode scheme\n"
    ))
    assert len(eng.evaluate_rule(elsewhere, rule)) == 1


def test_unindented_config_falls_back_to_separator_blocks():
    """捕获文本被压平（无缩进）时，仍应靠 # 分隔符正确切块。"""
    rule = next(r for r in eng.load_rules()["rules"] if r["id"] == "CFG-008")
    flat = "\n".join(l.strip() for l in (
        "interface Vlan-interface1",
        "ip address 192.168.1.1 255.255.255.0",
        "#",
        "interface Vlan-interface9",
        "description other",
    ))
    doc = eng.ConfigDocument("x", flat)
    assert doc.has_indent is False
    assert len(eng.evaluate_rule(doc, rule)) == 2


# ---------------------------------------------------------------------------
# 解析器细节
# ---------------------------------------------------------------------------

def test_prompt_prefix_and_separators_ignored():
    doc = eng.ConfigDocument("x", "<SW1>display current-configuration\n#\ntelnet server enable\n")
    assert doc.text == "display current-configuration\ntelnet server enable"
    assert len(doc.lines) == 2
    assert len(doc.all_lines) == 3  # 含 # 分隔符


def test_config_with_no_indent_flag_false():
    doc = eng.ConfigDocument("x", "sysname A\ntelnet server enable\n")
    assert doc.has_indent is False


def test_rule_exception_does_not_abort_audit():
    """单条规则的正则写坏时，其余规则仍应跑完。"""
    rs = eng.load_rules()
    broken = {
        "id": "CFG-999", "name": "坏规则", "severity": "low",
        "category": "security_audit", "match": "any", "patterns": ["([unclosed"],
    }
    rs2 = dict(rs, rules=[broken] + rs["rules"])
    findings, passed = eng.audit_document(_doc(HEALTHY), ruleset=rs2)
    by_id = {f["id"]: f for f in findings}
    assert "CFG-999" in by_id
    assert "规则执行异常" in by_id["CFG-999"]["hits"][0]["text"]
    # 其余规则不受影响：合规配置仍然全部通过
    assert set(by_id) == {"CFG-999"}
    assert "CFG-001" in {p["id"] for p in passed}
