"""CLI 与平台引擎的一致性（奇偶）校验。

背景：``tests/config_audit.py`` 为保持「零依赖、单文件可携带」的现场交付能力，
自带了一份与 ``app/services/config_audit_engine.py`` 等价的匹配实现。两份实现
一旦语义漂移，会出现「CLI 报合规、平台报不合规」这种最难排查的问题。

本文件用**同一批配置样本**分别跑两边，断言结论完全一致；规则集本身两边读的是
同一份 JSON（``backend/app/data/config_baseline_rules.json``），因此只需守住
**实现语义**不漂移。
"""
import importlib.util
import os
from pathlib import Path

import pytest

from app.services import config_audit_engine as eng

TESTS_DIR = Path(__file__).resolve().parent
CLI_PATH = TESTS_DIR / "config_audit.py"
FIXTURES = TESTS_DIR / "fixtures"


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("config_audit_cli", CLI_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 覆盖各 match 语义与边界形态的合成样本
SYNTHETIC = {
    "空配置": "",
    "仅分隔符": "#\n#\n",
    "华为 Vlanif1 带地址": (
        "#\ninterface Vlanif1\n ip address 10.0.0.1 255.255.255.0\n"
        " ip address 10.0.0.2 255.255.255.0 sub\n#\n"
    ),
    "压平无缩进": "interface Vlan-interface1\nip address 172.16.0.1 255.255.0.0\n",
    "VTY 有 scheme": "user-interface vty 0 4\n authentication-mode scheme\n protocol inbound ssh\n",
    "VTY 只有 line password": "user-interface vty 0 4\n authentication-mode password\n",
    "同账号两种最高权限写法": (
        "local-user a class manage\nlocal-user a privilege level 15\n"
    ),
    "两个最高权限账号": (
        "local-user a class manage\nlocal-user b privilege level 15\n"
    ),
    "community 名为 my-public-key": "snmp-agent community read cipher my-public-key\n",
    "community 为默认 public": "snmp-agent community read public\n",
    "带提示符的会话抓取": (
        "<SW1>display current-configuration\n#\ntelnet server enable\n#\nreturn\n"
    ),
    "多个 simple 口令": (
        "local-user a password simple Pass1\n"
        "local-user b password simple Pass2\n"
    ),
    "注释行不应参与匹配": "! telnet server enable\n#\n",
    "ip https 不应误报 http": "ip https server\n ip https ssl-server-policy p1\n",
}


def _platform(path_or_text, is_file):
    doc = (eng.ConfigDocument(str(path_or_text), path_or_text.read_text(encoding="utf-8"))
           if is_file else eng.ConfigDocument("inline", path_or_text))
    findings, passed = eng.audit_document(doc)
    return ({f["id"]: f for f in findings}, {p["id"] for p in passed})


def _cli(cli, path_or_text, is_file):
    doc = cli.load(str(path_or_text)) if is_file else cli.Config("inline", path_or_text)
    findings, passed, _ = cli.audit(doc, cli.load_rules())
    return ({f["id"]: f for f in findings}, {p["id"] for p in passed})


def _assert_same(cli, text_or_path, is_file=False):
    pf, pp = _platform(text_or_path, is_file)
    cf, cp = _cli(cli, text_or_path, is_file)
    assert set(pf) == set(cf), "命中集合不一致"
    assert pp == cp, "通过集合不一致"
    for rid in pf:
        assert pf[rid]["hits"] == cf[rid]["hits"], f"{rid} 的证据不一致"


@pytest.mark.parametrize("name", list(SYNTHETIC))
def test_synthetic_configs_agree(cli, name):
    _assert_same(cli, SYNTHETIC[name])


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.cfg")), ids=lambda p: p.name)
def test_fixture_configs_agree(cli, path):
    _assert_same(cli, path, is_file=True)


def test_both_sides_read_the_same_rules_file(cli):
    """两边必须读同一份 JSON，否则规则本身就会漂移。"""
    rs = cli.load_rules()
    assert os.path.abspath(rs["_source"]) == os.path.abspath(eng._RULES_PATH)
    assert len(rs["rules"]) == len(eng.load_rules()["rules"])


def test_cli_masks_secrets_too(cli):
    """打码不是平台独有行为，CLI 出的报告同样不能带明文口令。"""
    doc = cli.Config("x", "local-user a password simple Sup3rS3cret\n")
    findings, _, _ = cli.audit(doc, cli.load_rules())
    hits = next(f for f in findings if f["id"] == "CFG-003")["hits"]
    assert hits
    for h in hits:
        assert "Sup3rS3cret" not in h["text"]
        assert "******" in h["text"]


def test_cli_unmasked_mode_keeps_original(cli):
    doc = cli.Config("x", "local-user a password simple Sup3rS3cret\n")
    findings, _, _ = cli.audit(doc, cli.load_rules(), mask=False)
    hits = next(f for f in findings if f["id"] == "CFG-003")["hits"]
    assert any("Sup3rS3cret" in h["text"] for h in hits)


def test_export_rules_round_trip(cli, tmp_path, monkeypatch):
    """--export-rules 导出的文件应与真源逐字节一致，便于单文件携带。"""
    monkeypatch.setattr(cli, "SCRIPT_DIR", str(tmp_path))
    dst = cli.export_rules()
    assert Path(dst).is_file()
    assert Path(dst).read_bytes() == Path(eng._RULES_PATH).read_bytes()
    # 再导出一次不应报 SameFileError
    cli.export_rules()


def test_cli_reports_missing_rules_instead_of_silent_fallback(cli, tmp_path, monkeypatch):
    """找不到规则集必须报错，不能静默用一份过期内置规则出错误结论。"""
    monkeypatch.setattr(cli, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(cli.RULES_ENV_VAR, raising=False)
    with pytest.raises(cli.RuleError):
        cli.load_rules()


def test_cli_ignores_log_files(cli, tmp_path):
    """目录扫描不应把 .log 当配置文件。"""
    (tmp_path / "a.cfg").write_text("sysname A\n", encoding="utf-8")
    (tmp_path / "b.log").write_text("some log\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("sysname C\n", encoding="utf-8")
    found = [os.path.basename(p) for p in cli.collect([str(tmp_path)])]
    assert found == ["a.cfg", "c.txt"]
