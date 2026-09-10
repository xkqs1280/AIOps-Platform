"""等保配置基线核查服务 —— 把设备配置全文的合规结论落到合规检查表。

与 ``compliance_service`` 现有 SSH 运行态核查的关系是**互补**：

  - 本服务看**配置态**：配置里写了什么（telnet 是否 enable、口令是否明文、
    VTY 是否 scheme 认证）。数据源是平台已有的配置备份全文，无需新增采集。
  - 现有 ``SECONDARY_RULES`` 看**运行态/行为态**：服务当前是否真的在跑、
    最近有没有登录失败锁定、日志是否真的落地。这些在配置文本里看不到。

两者的 control_id 必须区分命名空间（本服务统一 ``CFG-*``，现有为 ``SEC-*``），
否则会撞 ``compliance_checks`` 表的 ``(device_id, control_id)`` 唯一约束、互相覆盖。

安全约定（重要）
----------------
``config_backups.config_content`` 是**含明文口令的完整配置**。本服务只把
「命中行号 + 已打码的片段」写入 ``compliance_checks.evidence``，
**绝不写入配置原文**；打码分两层：规则自带的 ``mask`` 正则（如 simple 明文口令），
再加 ``ai_service.sanitize_config`` 兜底（厂商密文 / PEM / community）。
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.models.config_backup import ConfigBackup
from app.models.device import Device
from app.models.p3_security import ComplianceCheck
from app.services.config_audit_engine import ConfigDocument, audit_document, load_rules

logger = logging.getLogger(__name__)


def _upsert_clause(model, values, index_elements):
    """按数据库方言构造幂等 upsert 语句。

    项目其余 upsert 直接用了 ``sqlalchemy.dialects.postgresql.insert``，在 SQLite
    上会生成 PG 专属语法而失败，导致这部分逻辑无法被单测覆盖。这里按方言选择
    构造器（与 ``database.py`` 建引擎时的分支判断保持同一约定），
    让配置基线核查的落库路径在 SQLite 与 PostgreSQL 上都能跑。
    """
    from app.database import engine

    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _insert
    else:
        from sqlalchemy.dialects.sqlite import insert as _insert

    stmt = _insert(model).values(values)
    return stmt.on_conflict_do_update(
        index_elements=index_elements,
        set_={k: getattr(stmt.excluded, k) for k in values if k not in set(index_elements)},
    )

# 单条证据的最大长度：够定位即可，避免把大段配置带进合规表
_HIT_TEXT_MAX = 120
# 每条规则证据总长度上限
_EVIDENCE_MAX = 500
# 单条规则最多列出的命中行数
_MAX_HITS = 6

CONFIG_SOURCE_BACKUP = "config_backup"
NOT_COLLECTED_REASON = "未采集到设备配置备份，无法进行配置基线核查"


def _sanitize(text: str) -> str:
    """兜底脱敏：即便规则 mask 漏了，也不让厂商密文/明文口令流出去。

    ``sanitize_config`` 依赖 ai_service，做惰性导入避免模块级循环依赖；
    该函数不存在时退化为不处理（此时仍已有规则级 mask 把关）。
    """
    try:
        from app.services.ai_service import sanitize_config
    except Exception:  # pragma: no cover - 仅在裁剪版部署中出现
        return text
    try:
        return sanitize_config(text)
    except Exception:  # pragma: no cover - 脱敏失败宁可截断也不放原文
        logger.warning("配置证据脱敏失败，已丢弃证据正文", exc_info=True)
        return "[证据脱敏失败，已隐藏]"


def _format_evidence(name: str, hits: list[dict], compliant: bool) -> str:
    """把命中项拼成可读且已脱敏的证据文本。"""
    if compliant:
        return f"配置中未检出「{name}」相关问题"
    if not hits:
        return "命中但无可用证据"

    parts, n_missing = [], 0
    for h in hits[:_MAX_HITS]:
        if h.get("missing"):
            # absent 类规则：命中即代表「该项缺失」，措辞不能用「命中」。
            n_missing += 1
            parts.append("配置中未找到该项")
            continue
        text = (h.get("text") or "")[:_HIT_TEXT_MAX]
        loc = f"行 {h['no']}" if h.get("no") else "全局"
        parts.append(f"{loc} `{text}`")
    if len(hits) > _MAX_HITS:
        parts.append(f"…另有 {len(hits) - _MAX_HITS} 处")

    if n_missing == len(hits):
        body = "未检出：" + "；".join(parts)
    else:
        body = "命中 %d 处：" % len(hits) + "；".join(parts)
    return _sanitize(body)[:_EVIDENCE_MAX]


async def _latest_successful_backup(session, device_id: int) -> ConfigBackup | None:
    """取该设备最近一次成功的配置备份。"""
    stmt = (
        select(ConfigBackup)
        .where(ConfigBackup.device_id == device_id, ConfigBackup.status == "success")
        .order_by(ConfigBackup.created_at.desc(), ConfigBackup.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def collect_findings(session, device_id: int) -> dict:
    """对单台设备执行配置基线核查，**不回写数据库**。

    Returns:
        dict: {
            device_id, device_name, ip,
            config_available: bool,          # 是否拿到配置全文
            config_backup_at: datetime|None, # 所用备份的时间
            details: [                       # 与运行态核查同构，便于前端合并展示
                {control_id, category, desc, status, evidence, source}
            ],
            note: str|None,                  # 未采集时的说明
        }
        设备不存在时返回 {"device_id": device_id, "error": "设备不存在"}。
    """
    device = (
        await session.execute(select(Device).where(Device.id == device_id))
    ).scalar_one_or_none()
    if device is None:
        return {"device_id": device_id, "error": "设备不存在"}

    ruleset = load_rules()
    backup = await _latest_successful_backup(session, device_id)

    details = []
    if backup is None or not (backup.config_content or "").strip():
        # 关键：配置没采集到 ≠ 不合规。判 not_applicable，否则刚接入的
        # 设备会整片飘红，客户会以为设备全是问题。
        for rule in ruleset["rules"]:
            details.append({
                "control_id": rule["id"],
                "category": rule.get("category") or "",
                "desc": rule["name"],
                "status": "not_applicable",
                "evidence": NOT_COLLECTED_REASON,
                "source": "config",
            })
        return {
            "device_id": device_id,
            "device_name": device.name,
            "ip": device.ip,
            "config_available": False,
            "config_backup_at": None,
            "details": details,
            "note": NOT_COLLECTED_REASON,
        }

    doc = ConfigDocument(f"{device.name}({device.ip})", backup.config_content)
    findings, passed = audit_document(doc, ruleset, mask=True)
    finding_by_id = {f["id"]: f for f in findings}
    passed_ids = {p["id"] for p in passed}

    # 保持规则集原始顺序，前端展示稳定
    for rule in ruleset["rules"]:
        rid = rule["id"]
        if rid in finding_by_id:
            f = finding_by_id[rid]
            status = "non_compliant"
            evidence = _format_evidence(f["name"], f["hits"], compliant=False)
        elif rid in passed_ids:
            status = "compliant"
            evidence = _format_evidence(rule["name"], [], compliant=True)
        else:  # pragma: no cover - audit_document 保证二者必居其一
            status = "not_applicable"
            evidence = "无法判定"
        details.append({
            "control_id": rid,
            "category": rule.get("category") or "",
            "desc": rule["name"],
            "status": status,
            "evidence": evidence,
            "source": "config",
        })

    return {
        "device_id": device_id,
        "device_name": device.name,
        "ip": device.ip,
        "config_available": True,
        "config_backup_at": backup.created_at,
        "config_line_count": backup.line_count or 0,
        "details": details,
        "note": None,
    }


def aggregate(collected: dict, categories: dict) -> dict:
    """把 details 聚合成评分 / 分类统计（与运行态核查口径一致）。

    Args:
        collected: ``collect_findings`` 的返回值。
        categories: {category_key: 中文名}，用于分类标签。
    """
    details = collected.get("details") or []
    applicable = [d for d in details if d["status"] != "not_applicable"]
    passed = sum(1 for d in applicable if d["status"] == "compliant")
    score = round(passed / len(applicable) * 100, 1) if applicable else 0.0

    cats = {}
    for key, label in categories.items():
        rows = [d for d in details if d.get("category") == key]
        ok = sum(1 for d in rows if d["status"] == "compliant")
        cats[key] = {
            "label": label,
            "score": round(ok / len(rows) * 100, 1) if rows else 0.0,
            "passed": ok,
            "total": len(rows),
        }

    return {
        "score": score,
        "passed": passed,
        "total": len(applicable),
        "details": details,
        "categories": cats,
    }


async def persist_details(session, device_id: int, details: list[dict]) -> int:
    """把核查结论 upsert 到 ``compliance_checks``（幂等）。

    注意：这里只写 control_id / 结论 / 已脱敏证据，**不写配置原文**。
    """
    now = datetime.now(timezone.utc)
    for d in details:
        values = {
            "device_id": device_id,
            "control_id": d["control_id"],
            "control_desc": d["desc"],
            "status": d["status"],
            "evidence": d["evidence"],
            "checked_at": now,
        }
        await session.execute(
            _upsert_clause(ComplianceCheck, values, ["device_id", "control_id"])
        )
    await session.commit()
    return len(details)


async def run_config_baseline_check(session, device_id: int) -> dict:
    """单台设备配置基线核查：采集结论 + 落库 + 聚合（可直接作为 API 响应）。

    Returns:
        dict: 与 ``compliance_service.run_secondary_compliance_check`` 结构对齐，
              另含 config_available / config_backup_at / methods 字段。
    """
    collected = await collect_findings(session, device_id)
    if collected.get("error"):
        return collected

    ruleset = load_rules()
    agg = aggregate(collected, ruleset.get("categories") or {})
    await persist_details(session, device_id, collected["details"])

    return {
        "device_id": device_id,
        "id": device_id,
        "device_name": collected["device_name"],
        "ip": collected["ip"],
        "method": CONFIG_SOURCE_BACKUP if collected["config_available"] else "none",
        "methods": [CONFIG_SOURCE_BACKUP] if collected["config_available"] else [],
        "config_available": collected["config_available"],
        "config_backup_at": collected["config_backup_at"],
        "checked_at": datetime.now(timezone.utc),
        "note": collected["note"],
        **agg,
    }
