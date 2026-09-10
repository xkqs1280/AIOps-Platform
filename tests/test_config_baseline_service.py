"""配置基线核查服务单测。

重点守住三条：
  1. **配置未采集 ≠ 不合规** —— 判 not_applicable，否则新接入设备整片飘红；
  2. **绝不把配置原文写进合规表** —— 配置里有明文口令，证据必须只留行号 + 已打码片段；
  3. **两类核查合并后口径一致** —— 评分/分类/数据来源标记要符合同一套规则。
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.config_backup import ConfigBackup
from app.models.p3_security import ComplianceCheck
from app.services import compliance_service as cs
from app.services import config_baseline_service as cbs
from app.services.config_audit_engine import load_rules

FIXTURES = Path(__file__).resolve().parent / "fixtures"
UNHEALTHY = (FIXTURES / "h3c_sw1_unhealthy.cfg").read_text(encoding="utf-8")
HEALTHY = (FIXTURES / "h3c_sw2_healthy.cfg").read_text(encoding="utf-8")

# 夹具里的明文口令，任何证据里都不允许出现
SECRETS = ("Admin@123", "Vty@123")


async def _add_backup(db, device, text, status="success", age_hours=0):
    b = ConfigBackup(
        device_id=device.id, backup_type="manual", config_content=text,
        line_count=text.count("\n") + 1, file_size=len(text), status=status,
        created_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
    )
    db.add(b)
    await db.commit()
    await db.refresh(b)
    return b


async def _rows(db, device_id):
    return (await db.execute(
        select(ComplianceCheck).where(ComplianceCheck.device_id == device_id)
    )).scalars().all()


# ---------------------------------------------------------------------------
# 配置未采集
# ---------------------------------------------------------------------------

async def test_no_backup_marks_all_not_applicable(db, device_factory):
    """没有配置备份时，14 项配置规则必须是 not_applicable，不能判不合规。"""
    dev = await device_factory(name="SW-new", ip="10.0.0.1")

    collected = await cbs.collect_findings(db, dev.id)

    assert collected["config_available"] is False
    assert collected["config_backup_at"] is None
    assert collected["note"] == cbs.NOT_COLLECTED_REASON
    assert len(collected["details"]) == len(load_rules()["rules"])
    assert {d["status"] for d in collected["details"]} == {"not_applicable"}

    agg = cbs.aggregate(collected, load_rules()["categories"])
    # 无适用项 → 不参与评分，而不是 0 分
    assert agg["total"] == 0
    assert agg["score"] == 0.0


async def test_empty_backup_content_is_treated_as_not_collected(db, device_factory):
    dev = await device_factory(name="SW-empty", ip="10.0.0.2")
    await _add_backup(db, dev, "   \n\n  ")
    collected = await cbs.collect_findings(db, dev.id)
    assert collected["config_available"] is False


async def test_failed_backup_is_ignored(db, device_factory):
    """失败的备份不能拿来当配置依据。"""
    dev = await device_factory(name="SW-fail", ip="10.0.0.3")
    await _add_backup(db, dev, UNHEALTHY, status="failed")
    collected = await cbs.collect_findings(db, dev.id)
    assert collected["config_available"] is False


async def test_latest_successful_backup_wins(db, device_factory):
    """同时存在新旧备份时用最新的那份。"""
    dev = await device_factory(name="SW-old", ip="10.0.0.4")
    await _add_backup(db, dev, UNHEALTHY, age_hours=10)
    await _add_backup(db, dev, HEALTHY, age_hours=1)
    collected = await cbs.collect_findings(db, dev.id)
    assert collected["config_available"] is True
    assert {d["status"] for d in collected["details"]} == {"compliant"}


# ---------------------------------------------------------------------------
# 结论正确性
# ---------------------------------------------------------------------------

async def test_unhealthy_config_all_non_compliant(db, device_factory):
    dev = await device_factory(name="SW-bad", ip="10.0.0.5")
    await _add_backup(db, dev, UNHEALTHY)

    collected = await cbs.collect_findings(db, dev.id)
    assert collected["config_available"] is True
    assert {d["status"] for d in collected["details"]} == {"non_compliant"}
    assert all(d["source"] == "config" for d in collected["details"])


async def test_healthy_config_all_compliant(db, device_factory):
    dev = await device_factory(name="SW-ok", ip="10.0.0.6")
    await _add_backup(db, dev, HEALTHY)

    collected = await cbs.collect_findings(db, dev.id)
    assert {d["status"] for d in collected["details"]} == {"compliant"}

    agg = cbs.aggregate(collected, load_rules()["categories"])
    assert agg["score"] == 100.0
    assert agg["passed"] == agg["total"] == len(load_rules()["rules"])


async def test_unknown_device_returns_error(db):
    assert (await cbs.collect_findings(db, 999999)).get("error") == "设备不存在"


# ---------------------------------------------------------------------------
# 安全：证据不得含配置原文
# ---------------------------------------------------------------------------

async def test_evidence_never_leaks_plaintext_secrets(db, device_factory):
    """这条是整块功能的红线：配置里明文口令绝不能进 compliance_checks。"""
    dev = await device_factory(name="SW-secret", ip="10.0.0.7")
    await _add_backup(db, dev, UNHEALTHY)
    await cbs.run_config_baseline_check(db, dev.id)

    rows = await _rows(db, dev.id)
    assert rows
    blob = "\n".join((r.evidence or "") for r in rows)
    for secret in SECRETS:
        assert secret not in blob, f"证据中泄漏了明文口令：{secret}"
    # 打码标记与行号定位应保留，报告才有可用性
    assert "******" in blob


async def test_evidence_is_bounded_not_a_config_dump(db, device_factory):
    """证据只应是行号 + 片段，不能把整份配置搬进去。"""
    dev = await device_factory(name="SW-dump", ip="10.0.0.8")
    await _add_backup(db, dev, UNHEALTHY)
    await cbs.run_config_baseline_check(db, dev.id)

    rows = await _rows(db, dev.id)
    for r in rows:
        assert len(r.evidence or "") <= cbs._EVIDENCE_MAX
    blob = "\n".join((r.evidence or "") for r in rows)
    # 未命中任何规则的配置行不应出现在证据里
    assert "port trunk permit vlan all" not in blob


async def test_absent_rule_evidence_not_worded_as_hit(db, device_factory):
    """缺失类规则(absent)命中代表「该项缺失」，证据措辞不能写成「命中」。"""
    dev = await device_factory(name="SW-absent", ip="10.0.0.10")
    await _add_backup(db, dev, UNHEALTHY)
    await cbs.run_config_baseline_check(db, dev.id)

    rows = {r.control_id: r for r in await _rows(db, dev.id)}
    # CFG-004「未启用 SSH 远程管理」为 absent 类，夹具中确实没有 ssh server
    ev = rows["CFG-004"].evidence or ""
    assert rows["CFG-004"].status == "non_compliant"
    assert ev.startswith("未检出"), ev
    assert "命中" not in ev, ev


async def test_no_compliance_row_stores_full_config(db, device_factory):
    dev = await device_factory(name="SW-full", ip="10.0.0.9")
    await _add_backup(db, dev, UNHEALTHY)
    await cbs.run_config_baseline_check(db, dev.id)
    rows = await _rows(db, dev.id)
    joined = " ".join((r.evidence or "") + (r.control_desc or "") for r in rows)
    assert "sysname HX-SW1" not in joined


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------

async def test_persist_is_idempotent(db, device_factory):
    dev = await device_factory(name="SW-idem", ip="10.0.0.10")
    await _add_backup(db, dev, UNHEALTHY)

    first = await cbs.run_config_baseline_check(db, dev.id)
    n1 = len(await _rows(db, dev.id))
    await cbs.run_config_baseline_check(db, dev.id)
    n2 = len(await _rows(db, dev.id))

    assert n1 == n2 == len(load_rules()["rules"])
    assert first["score"] == 0.0


async def test_control_ids_use_cfg_prefix_and_fit_column(db, device_factory):
    """CFG-* 前缀避免与运行态 SEC-* 撞唯一约束；长度要放得进 String(16)。"""
    dev = await device_factory(name="SW-id", ip="10.0.0.11")
    await _add_backup(db, dev, HEALTHY)
    await cbs.run_config_baseline_check(db, dev.id)

    ids = [r.control_id for r in await _rows(db, dev.id)]
    assert ids and all(i.startswith("CFG-") for i in ids)
    assert all(len(i) <= 16 for i in ids)


async def test_run_returns_api_shaped_payload(db, device_factory):
    dev = await device_factory(name="SW-api", ip="10.0.0.12")
    await _add_backup(db, dev, HEALTHY)

    res = await cbs.run_config_baseline_check(db, dev.id)
    assert res["device_id"] == dev.id
    assert res["method"] == "config_backup"
    assert res["methods"] == ["config_backup"]
    assert res["config_available"] is True
    assert res["score"] == 100.0
    assert set(res["categories"]) == set(load_rules()["categories"])


# ---------------------------------------------------------------------------
# 与运行态核查的合并
# ---------------------------------------------------------------------------

def _runtime_stub(details, method="ssh_config"):
    return {
        "device_id": 1, "id": 1, "device_name": "SW", "ip": "10.0.0.1",
        "method": method, "score": 0.0, "passed": 0, "total": len(details),
        "details": details, "categories": {},
    }


def _detail(cid, category, status, source=None):
    d = {"control_id": cid, "category": category, "desc": cid,
         "status": status, "evidence": ""}
    if source:
        d["source"] = source
    return d


def test_merge_combines_sources_and_recomputes_score():
    runtime = _runtime_stub([
        _detail("SEC-1.1", "identity_auth", "compliant"),
        _detail("SEC-1.2", "identity_auth", "non_compliant"),
    ])
    collected = {
        "config_available": True,
        "config_backup_at": datetime(2026, 9, 10, tzinfo=timezone.utc),
        "details": [
            _detail("CFG-001", "intrusion_prevention", "non_compliant", "config"),
            _detail("CFG-002", "data_confidentiality", "compliant", "config"),
        ],
        "note": None,
    }

    merged = cs._merge_device_results(runtime, collected)

    assert len(merged["details"]) == 4
    assert merged["total"] == 4
    assert merged["passed"] == 2
    assert merged["score"] == 50.0
    assert merged["methods"] == ["ssh_config", "config_backup"]
    assert merged["config_available"] is True
    # 运行态明细被自动打上 source 标记，前端才能区分来源
    assert [d["source"] for d in merged["details"]].count("runtime") == 2
    assert [d["source"] for d in merged["details"]].count("config") == 2
    # 分类合并且带 passed/total
    assert merged["categories"]["identity_auth"]["score"] == 50.0
    assert merged["categories"]["data_confidentiality"]["score"] == 100.0


def test_merge_without_config_keeps_runtime_score():
    """配置不可用时，配置类 14 项不应影响原有评分。"""
    runtime = _runtime_stub([
        _detail("SEC-1.1", "identity_auth", "compliant"),
        _detail("SEC-1.2", "identity_auth", "non_compliant"),
    ])
    collected = {
        "config_available": False, "config_backup_at": None, "note": cbs.NOT_COLLECTED_REASON,
        "details": [_detail("CFG-001", "intrusion_prevention", "not_applicable", "config")],
    }

    merged = cs._merge_device_results(runtime, collected)
    assert merged["total"] == 2
    assert merged["score"] == 50.0
    assert merged["methods"] == ["ssh_config"]
    assert merged["config_available"] is False
    assert merged["config_note"] == cbs.NOT_COLLECTED_REASON


def test_merge_passes_through_device_error():
    assert cs._merge_device_results({"device_id": 5, "error": "设备不存在"}, None) == {
        "device_id": 5, "error": "设备不存在",
    }


async def test_device_check_persists_config_results(db, device_factory, monkeypatch):
    """批量核查路径必须把配置结论落库。

    回归背景：最初只做了「合并」没做「落库」，结果是点完评估页面能看到配置项，
    一刷新（GET /status 读的是落库结论）配置项就"消失"了，且设备被标成
    config_available=False。这里用打桩绕开真实 SSH 采集来守住该契约。
    """
    dev = await device_factory(name="SW-persist", ip="10.0.0.30")
    await _add_backup(db, dev, UNHEALTHY)

    async def fake_runtime(session, device_id):
        runtime = {
            "device_id": device_id, "id": device_id, "device_name": dev.name,
            "ip": dev.ip, "method": "ssh_config", "score": 0.0, "passed": 0, "total": 1,
            "details": [_detail("SEC-1.1", "identity_auth", "compliant")],
            "categories": {},
        }
        # 真实实现会把自己的结论写库，这里照做，才能验证「两侧都落库」
        await cbs.persist_details(session, device_id, runtime["details"])
        return runtime

    monkeypatch.setattr(cs, "run_secondary_compliance_check", fake_runtime)

    merged = await cs.run_device_compliance_check(db, dev.id)
    rule_count = len(load_rules()["rules"])

    # 响应里两类结论都在，来源标记正确
    assert len(merged["details"]) == 1 + rule_count
    assert merged["methods"] == ["ssh_config", "config_backup"]
    assert merged["config_available"] is True

    # 关键：配置结论确实写进了库
    rows = await _rows(db, dev.id)
    cfg_ids = {r.control_id for r in rows if (r.control_id or "").startswith("CFG-")}
    assert len(cfg_ids) == rule_count

    # 刷新页面（状态查询）依然能看到配置结论
    st = await cs.get_compliance_status(db, device_id=dev.id)
    assert st["config_available"] is True
    assert st["total"] == 1 + rule_count
    assert st["methods"] == ["ssh_config", "config_backup"]


# ---------------------------------------------------------------------------
# 状态查询（读落库结论）
# ---------------------------------------------------------------------------

async def test_status_reads_persisted_results(db, device_factory):
    dev = await device_factory(name="SW-status", ip="10.0.0.20")
    await _add_backup(db, dev, UNHEALTHY)
    await cbs.run_config_baseline_check(db, dev.id)

    st = await cs.get_compliance_status(db)
    row = next(d for d in st["devices"] if d["device_id"] == dev.id)

    assert row["score"] == 0.0
    assert row["total"] == len(load_rules()["rules"])
    assert row["checked"] is True
    assert row["config_available"] is True
    assert row["methods"] == ["config_backup"]
    # 前端雷达要扁平数值，展开面板要对象形式，两者都要给到
    assert set(st["categories"]) == set(load_rules()["categories"])
    assert all(isinstance(v, int) for v in st["categories"].values())
    assert row["categories"]["identity_auth"]["label"] == "身份鉴别"
    assert isinstance(row["categories"]["identity_auth"]["score"], float)


async def test_status_excludes_legacy_mock_rows(db, device_factory):
    """早期基于指标推断写入的 8.1.* 行不得参与评分。"""
    dev = await device_factory(name="SW-legacy", ip="10.0.0.21")
    await _add_backup(db, dev, HEALTHY)
    await cbs.run_config_baseline_check(db, dev.id)

    legacy = ComplianceCheck(
        device_id=dev.id, control_id="8.1.3.2-a", control_desc="模拟项",
        status="non_compliant", evidence="指标推断",
    )
    db.add(legacy)
    await db.commit()

    st = await cs.get_compliance_status(db, device_id=dev.id)
    assert all(not d["control_id"].startswith("8.1.") for d in st["details"])
    assert st["score"] == 100.0


async def test_status_unchecked_device_is_not_zero_scored(db, device_factory):
    """未核查的设备不能显示成 0 分（会被当成设备有问题）。"""
    dev = await device_factory(name="SW-never", ip="10.0.0.22")
    st = await cs.get_compliance_status(db)
    row = next(d for d in st["devices"] if d["device_id"] == dev.id)
    assert row["checked"] is False
    assert row["score"] is None
    assert row["total"] == 0


async def test_status_single_device_and_missing(db, device_factory):
    dev = await device_factory(name="SW-one", ip="10.0.0.23")
    await _add_backup(db, dev, HEALTHY)
    await cbs.run_config_baseline_check(db, dev.id)

    one = await cs.get_compliance_status(db, device_id=dev.id)
    assert one["device_id"] == dev.id
    assert one["category_scores"]["identity_auth"] == 100.0
    assert await cs.get_compliance_status(db, device_id=999999) is None


async def test_status_does_not_leak_config_text(db, device_factory):
    """状态接口是前端直接展示的，同样不能带配置原文。"""
    dev = await device_factory(name="SW-leak", ip="10.0.0.24")
    await _add_backup(db, dev, UNHEALTHY)
    await cbs.run_config_baseline_check(db, dev.id)

    st = await cs.get_compliance_status(db, device_id=dev.id)
    blob = str(st)
    for secret in SECRETS:
        assert secret not in blob


# ---------------------------------------------------------------------------
# 分类体系一致性
# ---------------------------------------------------------------------------

def test_category_keys_match_runtime_checks():
    """五维分类键必须与运行态核查一致，否则前端雷达/分类统计会缺项。"""
    assert set(load_rules()["categories"]) == set(cs.SECONDARY_CATEGORIES)


@pytest.mark.parametrize("rule", load_rules()["rules"], ids=lambda r: r["id"])
def test_every_rule_is_wellformed(rule):
    assert rule["category"] in load_rules()["categories"]
    assert rule.get("advice")
    if rule["match"] in ("block", "block_absent"):
        assert rule.get("anchors") and rule.get("within")
    else:
        assert rule.get("patterns")
    if rule["match"] == "count":
        assert rule.get("min_hits")
