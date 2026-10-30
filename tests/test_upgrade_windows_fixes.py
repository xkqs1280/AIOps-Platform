# -*- coding: utf-8 -*-
"""Windows 升级链路修复的回归测试（2026-10-08 生产 4.5.7 → 4.5.9 事故）。

事故形态
--------
升级**实际已经成功**（文件替换完成、4.5.9 已起来并正常服务），但升级脚本的健康检查
只探 `/health`、且在固定窗口内探不到就写 `failed / health check timeout`，页面于是
显示"升级失败"；期间后端被停掉后也没能被可靠拉起（全靠人工 `start_hidden.vbs` 重启）。

本文件覆盖四组修复：
  1. 僵尸态判定不再只看时长 —— 脚本还活着时，长时间停在 verifying/90% 是正常等待，
     不能被误判成僵尸并解锁重入；
  2. 脚本存活探测在 Windows 上真实可用，且**不会自匹配**（`-Command` 自身命令行里就
     含有 "upgrade_apply" 字面量，是最初实现里的一个隐蔽回归）；
  3. 后端自愈在 Windows 上新增"补失败态"兜底，避免永久卡在中间态；
  4. `PythonPath` 在 exe（frozen）部署下不再传出 AIOpsServer.exe 自身。
"""
import inspect
import json
import os
from datetime import datetime, timedelta

import pytest

from app.services import upgrade_service as us


# ── 装置 ────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset_probe_cache():
    """模块级 TTL 缓存会串味，每个用例前后清一次。"""
    us._WIN_SCRIPT_PROBE_CACHE = None
    yield
    us._WIN_SCRIPT_PROBE_CACHE = None


@pytest.fixture()
def state_root(tmp_path, monkeypatch):
    monkeypatch.setattr(us, "get_upgrade_root", lambda: tmp_path)
    monkeypatch.setattr(us, "get_app_root", lambda: tmp_path)
    monkeypatch.setattr(us, "_state_file", lambda: tmp_path / "state.json")
    return tmp_path


def _write(root, **kw):
    data = {"state": "idle", "progress": 0, "message": "", "from_version": "4.5.7",
            "to_version": None, "started_at": None, "finished_at": None,
            "error": None, "rollback_available": False, "log": []}
    data.update(kw)
    (root / "state.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8-sig")


def _state(root):
    return json.loads((root / "state.json").read_text(encoding="utf-8-sig"))


def _ts(seconds_ago: float) -> str:
    return (datetime.now() - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%d %H:%M:%S")


# ── 1. 僵尸态判定 ───────────────────────────────────────────────────
def test_zombie_false_within_grace(state_root, monkeypatch):
    """刚发起升级（宽限期内）绝不算僵尸 —— 脚本可能才刚被 spawn。"""
    monkeypatch.setattr(us, "_upgrade_script_running", lambda: False)
    assert us._is_zombie_state({"state": "backup", "started_at": _ts(5)}) is False


def test_zombie_false_while_script_still_running(state_root, monkeypatch):
    """★ 核心修复：中间地带内脚本还活着 -> 不是僵尸。

    生产实测：脚本会在 verifying/90% 停留十几分钟（大库迁移 + onefile 冷启动），
    旧实现 300 秒就判僵尸并解锁重入，会让用户以为卡死而重复发起升级。
    """
    monkeypatch.setattr(us, "_upgrade_script_running", lambda: True)
    s = {"state": "verifying", "started_at": _ts(us._ZOMBIE_GRACE_SECONDS + 600)}
    assert us._is_zombie_state(s) is False


def test_zombie_true_when_script_gone(state_root, monkeypatch):
    """脚本已不在运行且已过宽限期 -> 僵尸，允许解锁重试。"""
    monkeypatch.setattr(us, "_upgrade_script_running", lambda: False)
    s = {"state": "verifying", "started_at": _ts(us._ZOMBIE_GRACE_SECONDS + 60)}
    assert us._is_zombie_state(s) is True


def test_zombie_true_past_hard_limit_even_if_probe_lies(state_root, monkeypatch):
    """绝对上限：超过 _ZOMBIE_HARD_TIMEOUT_SECONDS 无论如何当僵尸，防探测误报永久卡死。"""
    monkeypatch.setattr(us, "_upgrade_script_running", lambda: True)
    s = {"state": "verifying", "started_at": _ts(us._ZOMBIE_HARD_TIMEOUT_SECONDS + 60)}
    assert us._is_zombie_state(s) is True


def test_zombie_ignores_terminal_state():
    assert us._is_zombie_state({"state": "done", "started_at": _ts(100000)}) is False
    assert us._is_zombie_state({"state": "failed", "started_at": _ts(100000)}) is False


# ── 2. 脚本存活探测 ─────────────────────────────────────────────────
def test_script_running_true_when_apply_log_fresh(state_root):
    """快路径：apply.log 刚被写过 -> 认为脚本仍在运行（零开销）。"""
    (state_root / "apply.log").write_bytes(b"probe\n")
    assert us._upgrade_script_running() is True


def test_script_running_uses_process_probe_when_log_stale(state_root, monkeypatch):
    """apply.log 很旧 -> 退到进程探测（这里桩成 True）。"""
    monkeypatch.setattr(us.sys, "platform", "win32")
    log = state_root / "apply.log"
    log.write_bytes(b"stale\n")
    old = 1_000_000_000
    os.utime(log, (old, old))
    monkeypatch.setattr(us, "_win_script_probe_cached", lambda: True)
    assert us._upgrade_script_running() is True
    monkeypatch.setattr(us, "_win_script_probe_cached", lambda: False)
    assert us._upgrade_script_running() is False


def test_script_running_false_when_no_log_and_probe_finds_nothing(state_root, monkeypatch):
    monkeypatch.setattr(us.sys, "platform", "win32")
    monkeypatch.setattr(us, "_win_script_probe_cached", lambda: False)
    assert us._upgrade_script_running() is False


def test_win_probe_query_does_not_self_match():
    """回归：探测查询串里不能出现连续的 "upgrade_apply"。

    `powershell -Command "<query>"` 自身的命令行会包含该查询文本；若查询里写字面量
    `*upgrade_apply*`，它就会匹配到自己，永远返回「有脚本在跑」—— 自愈随之彻底失效。
    """
    src = inspect.getsource(us._win_script_probe_raw)
    assert "'*upgrade_' + 'apply*'" in src


def test_win_probe_failure_returns_false(monkeypatch):
    """探测失败 = 未能证明脚本仍在运行 -> False（宁可偶发抢写，也不要永久卡住）。"""
    def _boom(*_a, **_kw):
        raise OSError("powershell unavailable")

    monkeypatch.setattr(us.subprocess, "run", _boom)
    assert us._win_script_probe_raw() is False


# ── 3. Windows 补失败态兜底 ─────────────────────────────────────────
def test_reconcile_windows_marks_failed_when_stuck(state_root, monkeypatch):
    """脚本已消失 + 中间态超兜底窗口 + 新版本没生效 -> 补写失败态，而不是永久卡住。"""
    monkeypatch.setattr(us.sys, "platform", "win32")
    monkeypatch.setattr(us, "_upgrade_script_running", lambda: False)
    _write(state_root, state="verifying", progress=90, to_version="9.9.9",
           started_at=_ts(us._RECONCILE_FAIL_WINDOW_SECONDS + 60))
    st = us.reconcile_interrupted_upgrade()
    assert st is not None
    assert _state(state_root)["state"] == "failed"


def test_reconcile_windows_does_not_fail_within_window(state_root, monkeypatch):
    """还没到兜底窗口 -> 不插手（避免与仍在收尾的脚本抢写）。"""
    monkeypatch.setattr(us.sys, "platform", "win32")
    monkeypatch.setattr(us, "_upgrade_script_running", lambda: False)
    _write(state_root, state="verifying", progress=90, to_version="9.9.9",
           started_at=_ts(60))
    assert us.reconcile_interrupted_upgrade() is None
    assert _state(state_root)["state"] == "verifying"


def test_reconcile_windows_skips_when_no_started_at(state_root, monkeypatch):
    """无 started_at 时无从判断卡了多久 -> 不贸然判失败。"""
    monkeypatch.setattr(us.sys, "platform", "win32")
    monkeypatch.setattr(us, "_upgrade_script_running", lambda: False)
    _write(state_root, state="backup", progress=45, to_version="9.9.9", started_at=None)
    assert us.reconcile_interrupted_upgrade() is None
    assert _state(state_root)["state"] == "backup"


def test_reconcile_non_windows_never_marks_failed(state_root, monkeypatch):
    """Linux 侧保持「只补成功、不判失败」——那里的中间态由 cgroup 场景与僵尸超时兜底。"""
    monkeypatch.setattr(us.sys, "platform", "linux")
    _write(state_root, state="backup", progress=45, to_version="9.9.9",
           started_at=_ts(us._RECONCILE_FAIL_WINDOW_SECONDS + 600))
    us.reconcile_interrupted_upgrade()
    assert _state(state_root)["state"] == "backup"


# ── 4. PythonPath 不得把 exe 当解释器 ───────────────────────────────
def _capture_upgrade_cmd(state_root, monkeypatch):
    captured = {}

    def fake_spawn(cmd, cwd):
        captured["cmd"] = cmd

    script = state_root / "upgrade_apply.ps1"
    script.write_text("# stub")
    monkeypatch.setattr(us, "_spawn_detached", fake_spawn)
    monkeypatch.setattr(
        us, "validate_package",
        lambda _p: {"_staging": str(state_root / "staging"), "_resolved_version": "9.9.9"},
    )
    monkeypatch.setattr(us, "_apply_script", lambda: (script, ["powershell.exe", "-File"]))
    us.start_upgrade(state_root / "pkg.zip")
    return captured["cmd"]


def test_start_upgrade_passes_empty_pythonpath_when_frozen(state_root, monkeypatch):
    """exe 部署下 sys.executable 就是 AIOpsServer.exe —— 不能当解释器传给脚本。"""
    monkeypatch.setattr(us.sys, "frozen", True, raising=False)
    monkeypatch.setattr(us.sys, "executable", r"C:\AIOps-Windows\AIOpsServer.exe", raising=False)
    cmd = _capture_upgrade_cmd(state_root, monkeypatch)
    assert cmd[cmd.index("-PythonPath") + 1] == ""


def test_start_upgrade_passes_python_path_when_source(state_root, monkeypatch):
    """源码部署（非 frozen）仍需把 python 解释器路径传下去。"""
    monkeypatch.setattr(us.sys, "frozen", False, raising=False)
    monkeypatch.setattr(us.sys, "executable", r"C:\py\python.exe", raising=False)
    cmd = _capture_upgrade_cmd(state_root, monkeypatch)
    assert cmd[cmd.index("-PythonPath") + 1] == r"C:\py\python.exe"
