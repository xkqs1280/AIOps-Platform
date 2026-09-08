# -*- coding: utf-8 -*-
"""P1-8 回归：巡检重启对账 + 后台 loop 崩溃自愈。

loop_supervisor.supervise：
- 循环体抛异常 → 记录日志并延迟重启（不因单次崩溃永久消失）；
- 循环体意外 return（本应无限循环）→ 同样重启；
- 运行期间周期性刷新心跳（存活标记），退出后移除；
- 进程停机（取消监督任务）→ 取消内部循环并向上传播。

（巡检对账 reconcile_stale_inspection_tasks 依赖真实 DB，不在此单测；
其逻辑在测试服部署验证阶段覆盖。）
"""
import asyncio
import sys
import time
import unittest

sys.path.insert(0, "backend")

from app.services import loop_supervisor as ls  # noqa: E402


class TestSupervisor(unittest.TestCase):
    def tearDown(self):
        ls._heartbeats.clear()
        ls._supervisors.clear()

    def _run(self, coro):
        return asyncio.run(coro)

    def test_crash_then_restart_keeps_loop_alive(self):
        calls = {"n": 0}
        started = asyncio.Event()

        async def flaky_loop():
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("模拟崩溃")
            started.set()
            # 第三次起进入正常无限循环
            while True:
                await asyncio.sleep(0.05)

        async def scenario():
            task = asyncio.create_task(ls.supervise(
                "flaky", flaky_loop, restart_delay=0.01, heartbeat_interval=0.01
            ))
            await asyncio.wait_for(started.wait(), timeout=3)
            # 崩溃 2 次后应已重启并正常跑起来
            self.assertEqual(calls["n"], 3)
            self.assertIn("flaky", ls.heartbeats())  # 心跳存活标记
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertNotIn("flaky", ls.heartbeats())  # 退出后心跳移除

        self._run(scenario())

    def test_unexpected_return_restarts(self):
        calls = {"n": 0}
        started = asyncio.Event()

        async def returns_early():
            calls["n"] += 1
            if calls["n"] <= 2:
                return  # 意外 return（应无限循环却结束）
            started.set()
            while True:
                await asyncio.sleep(0.05)

        async def scenario():
            task = asyncio.create_task(ls.supervise(
                "early-ret", returns_early, restart_delay=0.01, heartbeat_interval=0.01
            ))
            await asyncio.wait_for(started.wait(), timeout=3)
            self.assertEqual(calls["n"], 3)  # 前两次 return 后都被重启
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self._run(scenario())

    def test_heartbeat_refreshes_while_running(self):
        async def steady_loop():
            while True:
                await asyncio.sleep(0.02)

        async def scenario():
            task = asyncio.create_task(ls.supervise(
                "steady", steady_loop, restart_delay=0.01, heartbeat_interval=0.02
            ))
            await asyncio.sleep(0.05)  # 等监督协程真正开始
            t0 = ls.heartbeats().get("steady")
            await asyncio.sleep(0.1)
            t1 = ls.heartbeats().get("steady")
            self.assertIsNotNone(t0)
            self.assertGreater(t1, t0)  # 心跳随时间推进而刷新
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self._run(scenario())

    def test_status_snapshot(self):
        async def steady_loop():
            while True:
                await asyncio.sleep(0.05)

        async def scenario():
            task = asyncio.create_task(ls.supervise(
                "snap", steady_loop, restart_delay=0.01, heartbeat_interval=0.01
            ))
            await asyncio.sleep(0.08)  # 等监督协程注册进 _supervisors
            st = ls.supervisor_status()
            self.assertIn("snap", st)
            self.assertTrue(st["snap"]["running"])
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self._run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
