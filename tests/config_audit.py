#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网络设备配置合规巡检工具 (Network Config Compliance Auditor)
============================================================
面向 H3C Comware / 华为 VRP 语系的交换机、路由器、防火墙配置文件，
做离线合规体检：识别安全与运维风险，输出风险清单 + 修复建议 + 巡检报告。

规则集与平台共用同一份 JSON
----------------------------
规则定义在 ``config_baseline_rules.json``（平台侧为
``backend/app/data/config_baseline_rules.json``，由
``backend/app/services/config_baseline_service.py`` 读取），CLI 与平台共用一份，
避免两套规则漂移。查找顺序：

  1) ``--rules`` 指定的路径
  2) 环境变量 ``CONFIG_AUDIT_RULES``
  3) 脚本同目录
  4) 仓库布局 ``<脚本目录>/../backend/app/data/``
  5) 当前工作目录及其 ``backend/app/data/``

找不到时**直接报错退出**，不静默退回过期内置规则（会导致错误的巡检结论）。
用 ``--export-rules`` 可把规则导出到脚本同目录，之后即可随脚本单文件携带。

特点
----
* 纯标准库，零依赖，拷到任何装了 Python 的机器上就能跑
* 离线分析：只需要配置文件，不需要连设备，不需要授权，不产生任何流量
* 输出可直接交付客户的 Markdown 巡检报告
* 命中的敏感值（如 simple 明文口令）默认打码后再输出，``--show-secrets`` 可关闭

用法
----
    python config_audit.py 配置.txt
    python config_audit.py 配置1.txt 配置2.txt --md 巡检报告.md
    python config_audit.py ./配置目录/ --md 报告.md
    python config_audit.py 配置.txt --show-secrets     # 证据保留原文（默认打码）
    python config_audit.py --export-rules             # 导出规则 JSON 到脚本同目录

作者：华哥 / 网络自动化工具箱
"""

import argparse
import io
import json
import os
import re
import shutil
import sys
from datetime import datetime

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


DEFAULT_RULES_FILENAME = "config_baseline_rules.json"
RULES_ENV_VAR = "CONFIG_AUDIT_RULES"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

SEV_LABEL = {"high": "高危", "medium": "中危", "low": "低危"}
SEV_WEIGHT = {"high": 20, "medium": 8, "low": 3}

# 会话抓取的行首提示符，如 <SW1>display current-configuration
_PROMPT_RE = re.compile(r"^\s*<[^>\r\n]{1,64}>\s*")


class RuleError(Exception):
    """规则集缺失或损坏。"""


# ---------------------------------------------------------------- 规则集

def _rule_candidates(explicit=None):
    if explicit:
        return [explicit]
    cands = []
    env = os.environ.get(RULES_ENV_VAR)
    if env:
        cands.append(env)
    cands.append(os.path.join(SCRIPT_DIR, DEFAULT_RULES_FILENAME))
    cands.append(os.path.join(SCRIPT_DIR, "..", "backend", "app", "data", DEFAULT_RULES_FILENAME))
    cands.append(os.path.join(os.getcwd(), DEFAULT_RULES_FILENAME))
    cands.append(os.path.join(os.getcwd(), "backend", "app", "data", DEFAULT_RULES_FILENAME))
    return [os.path.normpath(p) for p in cands]


def load_rules(explicit=None):
    """加载规则集；找不到或格式错误时抛 RuleError。"""
    tried = []
    for cand in _rule_candidates(explicit):
        tried.append(cand)
        if not os.path.isfile(cand):
            continue
        try:
            with io.open(cand, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise RuleError("规则集解析失败：%s（%s）" % (cand, e))
        rules = data.get("rules")
        if not isinstance(rules, list) or not rules:
            raise RuleError("规则集为空或格式错误：%s" % cand)
        for r in rules:
            if not r.get("id") or not r.get("match"):
                raise RuleError("规则条目缺少 id/match 字段：%r" % (r,))
        data.setdefault("categories", {})
        data.setdefault("severity_order", {"high": 0, "medium": 1, "low": 2})
        data["_source"] = cand
        return data
    raise RuleError(
        "未找到规则集 %s。已尝试：\n  %s\n"
        "请用 --rules 指定路径，或把规则文件放到脚本同目录。"
        % (DEFAULT_RULES_FILENAME, "\n  ".join(tried))
    )


def export_rules(explicit=None):
    """把规则集复制到脚本同目录，便于单文件携带。"""
    data = load_rules(explicit)
    dst = os.path.join(SCRIPT_DIR, DEFAULT_RULES_FILENAME)
    if os.path.abspath(data["_source"]) == os.path.abspath(dst):
        print("规则集已在脚本同目录：%s（%d 条规则）" % (dst, len(data["rules"])))
        return dst
    shutil.copyfile(data["_source"], dst)
    print("规则集已导出到：%s（%d 条规则）" % (dst, len(data["rules"])))
    return dst


# ---------------------------------------------------------------- 配置解析

class Line(object):
    """一行配置。indent 保留缩进，用于判定配置块边界。"""

    __slots__ = ("no", "text", "indent", "is_sep")

    def __init__(self, no, text, indent, is_sep=False):
        self.no = no
        self.text = text
        self.indent = indent
        self.is_sep = is_sep


class Config(object):
    """一份解析后的设备配置。

    同时保留两条序列：
      - ``all_lines``：含 ``#`` / ``!`` 分隔符，保持行序，用于判定配置块边界；
      - ``lines``：仅真实配置行，用于规则匹配。

    ``has_indent`` 记录全文是否存在缩进。H3C/华为 的
    ``display current-configuration`` 正常都带缩进；若捕获文本被压平过，
    则退化为「只按 ``#`` 分隔符切块」。
    """

    def __init__(self, path, text):
        self.path = path
        self.all_lines = []
        self.lines = []
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

    def find(self, pattern, flags=re.I):
        """返回 [(行号, 行内容), ...]，按行号升序。"""
        rx = re.compile(pattern, flags)
        return [(ln.no, ln.text) for ln in self.lines if rx.search(ln.text)]

    def has(self, pattern, flags=re.I):
        return bool(self.find(pattern, flags))

    def block_after(self, anchor):
        """返回 anchor 所在配置块内、位于 anchor 之后的行。

        块边界：遇到 ``#`` / ``!`` 分隔符，或缩进不大于 anchor 的下一行即结束。
        全文无缩进时只依据分隔符切块（H3C/华为 输出段间必有 ``#``）。
        """
        out = []
        try:
            start = self.all_lines.index(anchor)
        except ValueError:
            return out
        for ln in self.all_lines[start + 1:]:
            if ln.is_sep:
                break
            if self.has_indent and ln.indent <= anchor.indent:
                break
            out.append(ln)
        return out


def load(path):
    with io.open(path, "r", encoding="utf-8", errors="replace") as f:
        return Config(path, f.read())


# ---------------------------------------------------------------- 规则求值

def _flags(rule):
    return re.I if "i" in (rule.get("flags") or "i").lower() else 0


def _iter_matches(doc, rule):
    """逐行扫描，每行最多计一次命中（避免多个 pattern 重复计数）。"""
    compiled = [(p, re.compile(p, _flags(rule))) for p in (rule.get("patterns") or [])]
    for ln in doc.lines:
        for _, rx in compiled:
            m = rx.search(ln.text)
            if m:
                yield ln, m
                break


def _hits_of(doc, rule):
    kind = rule.get("match") or "any"

    if kind in ("block", "block_absent"):
        rx_a = [re.compile(p, _flags(rule)) for p in (rule.get("anchors") or [])]
        rx_w = [re.compile(p, _flags(rule)) for p in (rule.get("within") or [])]
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
    """按规则声明的 mask 正则给敏感值打码。

    mask 正则用捕获组圈定要保留的可读前缀（Python 的 re 不支持变长后顾断言）：
    命中后保留 group(1)，其余替换为 ``******``。
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
    """返回命中项 [{"no": int|None, "text": str}]；未命中返回 []。

    ``missing=True`` 表示「配置中缺失该项」（absent 类规则），
    与「命中某一行」区分，便于报告措辞。
    """
    out = []
    for ln in _hits_of(doc, rule):
        if ln is None:
            out.append({"no": None, "text": "未在配置中找到对应配置项", "missing": True})
        else:
            out.append({"no": ln.no, "text": mask_text(ln.text, rule) if mask else ln.text,
                        "missing": False})
    return out


def audit(doc, ruleset, mask=True):
    order = ruleset.get("severity_order") or {}
    findings, passed = [], []
    for rule in ruleset["rules"]:
        try:
            hits = evaluate_rule(doc, rule, mask=mask)
        except Exception as e:                      # 单条规则异常不应中断巡检
            hits = [{"no": None, "text": "规则执行异常：%s" % e}]
        item = {
            "id": rule["id"], "name": rule["name"],
            "severity": rule.get("severity") or "medium",
            "category": rule.get("category") or "",
            "advice": rule.get("advice") or "",
        }
        if hits:
            findings.append(dict(item, hits=hits))
        else:
            passed.append(item)
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["id"]))
    score = 100
    for f in findings:
        score -= SEV_WEIGHT.get(f["severity"], 5)
    return findings, passed, max(score, 0)


def grade(score):
    if score >= 90:
        return "良好"
    if score >= 75:
        return "一般"
    if score >= 60:
        return "偏低"
    return "差"


def category_stats(ruleset, findings, passed):
    """按五维分类统计 (通过数, 总数)。"""
    cats = ruleset.get("categories") or {}
    stats = {k: [0, 0] for k in cats}
    for item in findings:
        c = item.get("category")
        if c in stats:
            stats[c][1] += 1
    for item in passed:
        c = item.get("category")
        if c in stats:
            stats[c][0] += 1
            stats[c][1] += 1
    return stats


# ---------------------------------------------------------------- 输出

INFO_CHECKS = [
    ("设备版本 / 型号", r"^H3C\s+Comware\s+Software,\s*Version\s+\S+|"
                        r"^\s*version\s+\S+|^Huawei\s+Versatile\s+Routing\s+Platform"),
    ("已配置的管理地址", r"^ip\s+address\s+\S+"),
    ("SSH 服务", r"^ssh\s+server\s+enable"),
    ("SNMP 配置", r"^snmp-agent"),
    ("AAA / 认证域", r"^domain\s+\S+|^hwtacacs|^radius\s+scheme"),
]


def build_report(cfg, ruleset, findings, passed, score):
    cats = ruleset.get("categories") or {}
    L = []
    A = L.append
    A("# 网络设备配置合规巡检报告")
    A("")
    A("| 项目 | 内容 |")
    A("|---|---|")
    A("| 巡检对象 | `%s` |" % os.path.basename(cfg.path))
    A("| 巡检时间 | %s |" % datetime.now().strftime("%Y-%m-%d %H:%M"))
    A("| 配置行数 | %d 行（不含注释与分隔符） |" % len(cfg.lines))
    A("| 规则集 | %s v%s |" % (ruleset.get("spec", "配置基线核查"),
                              ruleset.get("version", "-")))
    A("| 健康评分 | **%d / 100**（%s） |" % (score, grade(score)))
    A("| 风险统计 | 高危 %d · 中危 %d · 低危 %d |" % (
        sum(1 for f in findings if f["severity"] == "high"),
        sum(1 for f in findings if f["severity"] == "medium"),
        sum(1 for f in findings if f["severity"] == "low")))
    A("| 已通过检查 | %d 项 |" % len(passed))
    A("")

    A("## 一、分类符合情况")
    A("")
    A("| 分类 | 通过 / 总数 | 符合率 |")
    A("|---|---|---|")
    for key, label in cats.items():
        ok, total = category_stats(ruleset, findings, passed).get(key, [0, 0])
        rate = "%.0f%%" % (ok * 100.0 / total) if total else "—"
        A("| %s | %d / %d | %s |" % (label, ok, total, rate))
    A("")

    A("## 二、风险清单")
    A("")
    if not findings:
        A("> 未发现风险项。")
        A("")
    for i, f in enumerate(findings, 1):
        A("### %d. [%s] %s · %s" % (i, SEV_LABEL.get(f["severity"], f["severity"]),
                                    f["id"], f["name"]))
        A("")
        A("分类：%s" % cats.get(f["category"], f["category"] or "—"))
        A("")
        A("**证据**")
        A("")
        for h in f["hits"][:6]:
            if h.get("missing"):
                A("- 未检出：配置中未找到该项")
                continue
            loc = "行 %s" % h["no"] if h["no"] else "全局"
            A("- %s：`%s`" % (loc, h["text"]))
        if len(f["hits"]) > 6:
            A("- …另有 %d 处" % (len(f["hits"]) - 6))
        A("")
        A("**修复建议**：%s" % f["advice"])
        A("")

    A("## 三、已通过检查")
    A("")
    for p in passed:
        A("- ✅ %s %s（%s）" % (p["id"], p["name"], cats.get(p["category"], "—")))
    A("")

    A("## 四、设备信息（供人工核对）")
    A("")
    for label, pat in INFO_CHECKS:
        hits = cfg.find(pat)
        if hits:
            A("- **%s**：%s" % (label, "；".join("`%s`" % l for _, l in hits[:3])))
        else:
            A("- **%s**：未检出" % label)
    A("")
    A("---")
    A("")
    A("本报告基于离线配置文件静态分析，不连接设备、不产生任何流量。")
    A("建议修复后重新巡检，直至高危与中危清零。")
    return "\n".join(L)


def print_console(cfg, ruleset, findings, passed, score):
    print("")
    print("=" * 66)
    print(" 配置合规巡检 · %s" % os.path.basename(cfg.path))
    print("=" * 66)
    print(" 健康评分: %d/100 (%s)   配置行数: %d" % (score, grade(score), len(cfg.lines)))
    print(" 高危 %d · 中危 %d · 低危 %d · 通过 %d" % (
        sum(1 for f in findings if f["severity"] == "high"),
        sum(1 for f in findings if f["severity"] == "medium"),
        sum(1 for f in findings if f["severity"] == "low"),
        len(passed)))
    cats = ruleset.get("categories") or {}
    stats = category_stats(ruleset, findings, passed)
    brief = "  ".join(
        "%s %d/%d" % (label, stats.get(k, [0, 0])[0], stats.get(k, [0, 0])[1])
        for k, label in cats.items() if stats.get(k, [0, 0])[1]
    )
    if brief:
        print(" 分类: %s" % brief)
    print("-" * 66)
    if not findings:
        print(" 未发现风险项。")
    for f in findings:
        h0 = f["hits"][0] if f["hits"] else {}
        if h0.get("missing"):
            loc = "未检出"
        elif h0.get("no"):
            loc = "行 %s" % h0["no"]
        else:
            loc = "全局"
        print(" [%s] %-8s %s  (%s)" % (
            SEV_LABEL.get(f["severity"], f["severity"]), f["id"], f["name"], loc))
    print("-" * 66)
    print(" 提示：加 --md 报告.md 可输出可直接交付客户的完整巡检报告")
    print("")


# ---------------------------------------------------------------- 入口

def collect(paths):
    """收集待巡检的配置文件。只认设备配置常见的扩展名，不误扫日志。"""
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, names in os.walk(p):
                for n in names:
                    if n.lower().endswith((".cfg", ".txt", ".conf", ".config")):
                        files.append(os.path.join(root, n))
        elif os.path.isfile(p):
            files.append(p)
    return sorted(files)


def main():
    ap = argparse.ArgumentParser(
        description="网络设备配置合规巡检工具（H3C Comware / 华为 VRP）")
    ap.add_argument("paths", nargs="*", help="配置文件或目录")
    ap.add_argument("--md", metavar="FILE", help="输出 Markdown 巡检报告")
    ap.add_argument("--rules", metavar="FILE", help="指定规则集 JSON 路径")
    ap.add_argument("--export-rules", action="store_true",
                    help="把规则集导出到脚本同目录（便于单文件携带）")
    ap.add_argument("--show-secrets", action="store_true",
                    help="证据保留原文（默认对 simple 明文口令等敏感值打码）")
    ap.add_argument("--list-rules", action="store_true", help="列出全部规则后退出")
    args = ap.parse_args()

    try:
        if args.export_rules:
            export_rules(args.rules)
            return
        ruleset = load_rules(args.rules)
    except RuleError as e:
        print("错误：%s" % e, file=sys.stderr)
        sys.exit(2)

    if args.list_rules:
        cats = ruleset.get("categories") or {}
        print("规则集：%s（v%s）" % (ruleset["_source"], ruleset.get("version", "-")))
        for r in ruleset["rules"]:
            print("  %-9s %-7s %-8s %s" % (
                r["id"], r.get("severity", "-"),
                cats.get(r.get("category"), "-"), r["name"]))
        return

    if not args.paths:
        ap.error("请指定配置文件或目录（--help 查看用法）")

    files = collect(args.paths)
    if not files:
        print("未找到可巡检的配置文件（.cfg / .txt / .conf / .config）")
        sys.exit(1)

    mask = not args.show_secrets
    reports = []
    for fp in files:
        cfg = load(fp)
        findings, passed, score = audit(cfg, ruleset, mask=mask)
        print_console(cfg, ruleset, findings, passed, score)
        reports.append((cfg, ruleset, findings, passed, score))

    if args.md:
        parts = [build_report(*r) for r in reports]
        with io.open(args.md, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n\n\n".join(parts))
        print("巡检报告已生成: %s" % args.md)

    if mask:
        print("说明：证据中的明文口令等敏感值已打码；如需原文加 --show-secrets")


if __name__ == "__main__":
    main()
