# -*- coding: utf-8 -*-
"""P0-2 回归：告警引擎重启后僵尸 active 告警可在条件恢复时自动 resolve。

核心改动：_eval_scalar/_eval_if_status/_eval_if_errors 的 resolve 不再被
"内存态 violating 转换门"包住——当前条件不违规即无条件尝试恢复（无 active
行时为 no-op），杜绝引擎重启/升级后告警永久停留 active。
"""
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, "backend")

import app.services.alert_rule_engine as engine


def _device(over=None):
    d = SimpleNamespace(
        id=1, name="SW-TEST", ip="192.168.124.9", status="online",
        cpu_usage=5.0, memory_usage=10.0, temperature=30.0,
    )
    if over:
        for k, v in over.items():
            setattr(d, k, v)
    return d


def _rule(metric="cpu_usage", threshold=80, condition="gte", duration=30, severity="warning"):
    return SimpleNamespace(
        id=10, name="CPU过高测试", metric=metric, threshold=threshold,
        condition=condition, duration=duration, severity=severity,
    )


class TestScalarZombie(unittest.TestCase):
    def setUp(self):
        engine._state.clear()

    @mock.patch.object(engine, "_resolve_alert", new=mock.AsyncMock())
    @mock.patch.object(engine, "_ensure_alert", new=mock.AsyncMock())
    def test_restart_zombie_resolved_when_condition_recovered(self):
        # 模拟引擎重启：_state 为空，DB 遗留 active 告警，当前值已恢复（<阈值）
        import asyncio
        db = object()
        dev = _device()
        rule = _rule()
        asyncio.run(engine._eval_scalar(db, dev, rule, 45.0))
        engine._resolve_alert.assert_awaited_once_with(db, dev, rule.name)
        st = engine._state[(dev.id, rule.id)]
        self.assertFalse(st["violating"])

    @mock.patch.object(engine, "_resolve_alert", new=mock.AsyncMock())
    @mock.patch.object(engine, "_ensure_alert", new=mock.AsyncMock())
    def test_still_violating_keeps_active_no_resolve(self):
        import asyncio
        db = object()
        dev = _device()
        rule = _rule()
        asyncio.run(engine._eval_scalar(db, dev, rule, 95.0))
        engine._resolve_alert.assert_not_called()
        st = engine._state[(dev.id, rule.id)]
        self.assertTrue(st["violating"])

    @mock.patch.object(engine, "_resolve_alert", new=mock.AsyncMock())
    @mock.patch.object(engine, "_ensure_alert", new=mock.AsyncMock())
    def test_normal_recovery_still_resolves(self):
        import asyncio
        db = object()
        dev = _device()
        rule = _rule()
        # 先违规一轮（violating=True），再恢复 → 应 resolve
        asyncio.run(engine._eval_scalar(db, dev, rule, 95.0))
        asyncio.run(engine._eval_scalar(db, dev, rule, 30.0))
        engine._resolve_alert.assert_awaited_once_with(db, dev, rule.name)
        self.assertFalse(engine._state[(dev.id, rule.id)]["violating"])


class TestIfStatusZombie(unittest.TestCase):
    def setUp(self):
        engine._state.clear()
        engine._if_prev_status.clear()
        engine._if_down_since.clear()

    @mock.patch.object(engine, "_resolve_alert", new=mock.AsyncMock())
    @mock.patch.object(engine, "_ensure_alert", new=mock.AsyncMock())
    def test_restart_zombie_resolved_no_down_ifaces_now(self):
        import asyncio
        db = object()
        dev = _device()
        rule = _rule(metric="if_oper_status")
        # 重启后空状态：当前无任何 down 转换接口 → 遗留 active 应被 resolve
        asyncio.run(engine._eval_if_status(db, dev, rule, {}, {}))
        engine._resolve_alert.assert_awaited_once_with(db, dev, rule.name)


class TestIfErrorsZombie(unittest.TestCase):
    def setUp(self):
        engine._state.clear()
        engine._if_err_since.clear()
        engine._if_err_prev.clear()

    @mock.patch.object(engine, "_resolve_alert", new=mock.AsyncMock())
    @mock.patch.object(engine, "_ensure_alert", new=mock.AsyncMock())
    def test_restart_zombie_resolved_no_err_deltas_now(self):
        import asyncio
        db = object()
        dev = _device()
        rule = _rule(metric="if_in_errors")
        asyncio.run(engine._eval_if_errors(db, dev, rule, {}, {}))
        engine._resolve_alert.assert_awaited_once_with(db, dev, rule.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
