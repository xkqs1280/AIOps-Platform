# -*- coding: utf-8 -*-
"""升级状态自愈 reconcile_interrupted_upgrade 的回归测试。

背景（2026-09-21 实测）：Linux 升级脚本是后端子进程，与 uvicorn 同属
aiops-backend.service 的 cgroup（KillMode=control-group）。脚本 pkill 停服后，
systemd 按 Restart=always 重启服务时会清空整个 cgroup，把正在做健康检查的脚本
一起杀掉 —— 没人写 done，状态永久停在 verifying/90%，页面表现为"升级卡住"。

后端启动自愈负责在「服务已经起来 + 运行版本 == 升级目标版本」时补写 done。
判据刻意保守：只补成功、不判失败（未生效的中间态交给既有的僵尸态超时解锁）。
"""
import json

import pytest

from app.services import upgrade_service as us


@pytest.fixture()
def state_root(tmp_path, monkeypatch):
    monkeypatch.setattr(us, "get_upgrade_root", lambda: tmp_path)
    return tmp_path


def _write(root, **kw):
    data = {"state": "idle", "progress": 0, "message": "", "from_version": "4.5.6",
            "to_version": None, "started_at": None, "finished_at": None,
            "error": None, "rollback_available": False, "log": []}
    data.update(kw)
    (root / "state.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8-sig")


def _state(root):
    return json.loads((root / "state.json").read_text(encoding="utf-8-sig"))


def test_reconcile_writes_done_when_target_version_running(state_root):
    """服务已起来且运行版本就是升级目标版本 -> 补写 done。"""
    _write(state_root, state="verifying", progress=90, message="Waiting for service health",
           to_version=us.APP_VERSION)
    (state_root / "backup").mkdir()
    us.reconcile_interrupted_upgrade()
    s = _state(state_root)
    assert s["state"] == "done"
    assert s["progress"] == 100
    assert s["rollback_available"] is True


def test_reconcile_skips_when_target_version_not_active(state_root):
    """新代码还没起效时不能误判成功，否则会掩盖真正的升级失败。"""
    _write(state_root, state="backup", progress=45, to_version="9.9.9")
    us.reconcile_interrupted_upgrade()
    assert _state(state_root)["state"] == "backup"


def test_reconcile_skips_terminal_state(state_root):
    _write(state_root, state="done", progress=100, to_version=us.APP_VERSION)
    us.reconcile_interrupted_upgrade()
    assert _state(state_root)["progress"] == 100


def test_reconcile_skips_when_to_version_missing(state_root):
    _write(state_root, state="restarting", progress=80, to_version=None)
    us.reconcile_interrupted_upgrade()
    assert _state(state_root)["state"] == "restarting"


def test_reconcile_tolerates_missing_state_file(state_root):
    """全新部署没有 state.json，不能抛异常（它在 lifespan 里被调用）。"""
    assert us.reconcile_interrupted_upgrade() is None
