"""等保配置基线核查引擎 —— 对设备配置全文做结构化合规检查。

与告警引擎、指标采集完全解耦：纯函数、无 IO、无 DB 依赖，输入配置文本、
输出命中项，便于单测与复用。

规则集来自 ``app/data/config_baseline_rules.json``（GB/T 22239-2019 等保2.0）。
CLI 工具 ``tests/config_audit.py`` 另有一份等价实现，用于保持「零依赖单文件可
携带」的交付能力；``tests/test_config_audit_parity.py`` 用同一批配置样本断言
两者结论一致，防止规则语义漂移。

与现有 SSH 运行态核查（``compliance_service.SECONDARY_RULES``）的关系是互补而非
替代：本引擎只能看到**配置态**（配置文本里写了什么），看不到运行态（服务当前是否
真的在跑、最近有没有登录失败锁定），后者仍由 SSH 采集判断。
"""

import json
import os
import re

_RULES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "config_baseline_rules.json",
)

# 会话抓取的行首提示符，如 <SW1>display current-configuration
_PROMPT_RE = re.compile(r"^\s*<[^>\r\n]{1,64}>\s*")

_rules_cache = None


class ConfigRuleError(Exception):
    """规则集缺失或损坏。"""


def load_rules(path=None):
    """加载规则集（进程内缓存）。

    Raises:
        ConfigRuleError: 文件不存在或 JSON/字段不合法。
    """
    global _rules_cache
    if path is None and _rules_cache is not None:
        return _rules_cache

    target = path or _RULES_PATH
    if not os.path.isfile(target):
        raise ConfigRuleError(f"规则集文件不存在：{target}")
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigRuleError(f"规则集解析失败：{target}（{e}）") from e

    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ConfigRuleError(f"规则集为空或格式错误：{target}")
    for r in rules:
        if not r.get("id") or not r.get("match"):
            raise ConfigRuleError(f"规则条目缺少 id/match 字段：{r!r}")
    data.setdefault("categories", {})
    data.setdefault("severity_order", {"high": 0, "medium": 1, "low": 2})

    if path is None:
        _rules_cache = data
    return data


# ---------------------------------------------------------------------------
# 配置文本解析
# ---------------------------------------------------------------------------

class Line:
    """一行配置。``indent`` 保留缩进用于判定配置块边界。"""

    __slots__ = ("no", "text", "indent", "is_sep")

    def __init__(self, no, text, indent, is_sep=False):
        self.no = no
        self.text = text
        self.indent = indent
        self.is_sep = is_sep


class ConfigDocument:
    """一份解析后的设备配置。

    同时保留两条序列：
      - ``all_lines``：含 ``#`` / ``!`` 分隔符，保持行序，用于判定配置块边界；
      - ``lines``：仅真实配置行，用于规则匹配。

    ``has_indent`` 记录全文是否存在缩进。H3C/华为 的 ``display current-configuration``
    正常都带缩进；若捕获到的文本被压平过，则退化为「只按 ``#`` 分隔符切块」。
    """

    def __init__(self, path, text):
        self.path = path
        self.all_lines = []
        self.lines = []
        self.text = ""
        for i, raw in enumerate(text.splitlines(), 1):
            body = _PROMPT_RE.sub("", raw)
            s = body.strip()
            if not s:
                continue
            if s.startswith("#") or s.startswith("!"):
                self.all_lines.append(Line(i, s, 0, True))
                continue
            indent = len(body) - len(body.lstrip())
            ln = Line(i, s, indent, False)
            self.all_lines.append(ln)
            self.lines.append(ln)
        self.has_indent = any(ln.indent > 0 for ln in self.lines)
        self.text = "\n".join(ln.text for ln in self.lines)

    # -- 基础查询 ---------------------------------------------------------

    def find(self, pattern, flags=re.I):
        """返回 [(行号, 行内容), ...]，按行号升序。"""
        rx = re.compile(pattern, flags)
        return [(ln.no, ln.text) for ln in self.lines if rx.search(ln.text)]

    def has(self, pattern, flags=re.I):
        return bool(self.find(pattern, flags))

    def block_after(self, anchor):
        """返回 ``anchor`` 所在配置块内、位于 anchor 之后的行。

        块边界：遇到 ``#`` / ``!`` 分隔符，或缩进不大于 anchor 的下一行即结束。
        全文无缩进时只依据分隔符切块（H3C/华为 输出段间必有 ``#``）。
        """
        out = []
        start = self.all_lines.index(anchor) if anchor in self.all_lines else -1
        if start < 0:
            return out
        for ln in self.all_lines[start + 1:]:
            if ln.is_sep:
                break
            if self.has_indent and ln.indent <= anchor.indent:
                break
            out.append(ln)
        return out


# ---------------------------------------------------------------------------
# 规则求值
# ---------------------------------------------------------------------------

def _match_flags(rule):
    return re.I if "i" in (rule.get("flags") or "i").lower() else 0


def _iter_matches(doc, rules):
    """逐行扫描，每行最多计一次命中（避免多个 pattern 重复计数）。"""
    compiled = [(p, re.compile(p, _match_flags(rules))) for p in (rules.get("patterns") or [])]
    for ln in doc.lines:
        for _, rx in compiled:
            m = rx.search(ln.text)
            if m:
                yield ln, m
                break


def _hits_of(rule, doc):
    """返回该规则的命中证据行列表。"""
    kind = rule.get("match") or "any"

    if kind in ("block", "block_absent"):
        rx_a = [re.compile(p, _match_flags(rule)) for p in (rule.get("anchors") or [])]
        rx_w = [re.compile(p, _match_flags(rule)) for p in (rule.get("within") or [])]
        hits = []
        for ln in doc.lines:
            if not any(rx.search(ln.text) for rx in rx_a):
                continue
            block = doc.block_after(ln)
            matched = [b for b in block if any(rx.search(b.text) for rx in rx_w)]
            if kind == "block" and matched:
                hits.append(ln)
                hits.extend(matched)
            elif kind == "block_absent" and not matched:
                hits.append(ln)
        return hits

    matched = list(_iter_matches(doc, rule))
    if kind == "absent":
        return [] if matched else [None]
    if kind == "count":
        group = rule.get("distinct_group")
        if group:
            seen, uniq = set(), []
            for ln, m in matched:
                try:
                    key = m.group(group)
                except (IndexError, re.error):
                    key = ln.no
                if key not in seen:
                    seen.add(key)
                    uniq.append((ln, m))
            matched = uniq
        min_hits = int(rule.get("min_hits") or 1)
        return [ln for ln, _ in matched] if len(matched) >= min_hits else []
    # 默认 any
    return [ln for ln, _ in matched]


def mask_text(text, rule):
    """按规则声明的 mask 正则给敏感值打码（如 simple 明文口令）。

    mask 正则用**捕获组**圈定要保留的可读前缀（Python 的 re 不支持变长后顾断言，
    故不用 lookbehind）：命中后保留 group(1)、其余部分替换为 ``******``。
    例：``(\\bpassword\\s+simple\\s+)\\S+`` → ``password simple ******``。
    """
    for p in rule.get("mask") or []:
        text = re.sub(
            p,
            lambda m: (m.group(1) if m.groups() else "") + "******",
            text,
            flags=re.I,
        )
    return text


def evaluate_rule(doc, rule, mask=True):
    """对单条规则求值。

    Returns:
        list[dict]: 命中项 ``{"no": int|None, "text": str}``；
                    ``no`` 为 None 表示「该项缺失」（absent 类规则）。
                    未命中返回空列表。
    """
    hits = _hits_of(rule, doc)
    out = []
    for ln in hits:
        if ln is None:
            # missing=True 表示「该项在配置中缺失」，而非「命中了某行」。
            # 上层据此区分证据措辞，避免把缺失写成「命中」。
            out.append({"no": None, "text": "未在配置中找到对应配置项", "missing": True})
        else:
            text = mask_text(ln.text, rule) if mask else ln.text
            out.append({"no": ln.no, "text": text, "missing": False})
    return out


def audit_document(doc, ruleset=None, mask=True):
    """对整份配置执行全部规则。

    Returns:
        tuple[list[dict], list[dict]]: (findings, passed)
        findings 每项：{id, name, severity, category, advice, hits}
        passed 每项：{id, name, severity, category}
    """
    rs = ruleset or load_rules()
    order = rs.get("severity_order") or {}
    findings, passed = [], []
    for rule in rs["rules"]:
        try:
            hits = evaluate_rule(doc, rule, mask=mask)
        except Exception as e:  # 单条规则异常不应中断整轮核查
            hits = [{"no": None, "text": f"规则执行异常：{e}"}]
        item = {
            "id": rule["id"],
            "name": rule["name"],
            "severity": rule.get("severity") or "medium",
            "category": rule.get("category") or "",
            "advice": rule.get("advice") or "",
        }
        if hits:
            findings.append(dict(item, hits=hits))
        else:
            passed.append(item)
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["id"]))
    return findings, passed
