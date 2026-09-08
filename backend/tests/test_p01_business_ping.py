# -*- coding: utf-8 -*-
"""P0-1 行为单测：_ping_ip 三态 + _probe_single None 跳过逻辑（不连库）。"""
import asyncio
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, "backend")

from app.services.business_monitor_service import (
    _ping_ip,
    _probe_single,
    probe_terminal_online,
)


def _fake_run_result(stdout: bytes, stderr: bytes = b"", rc: int = 0):
    p = mock.Mock()
    p.stdout = stdout
    p.stderr = stderr
    p.returncode = rc
    return p


class TestPingIpTristate(unittest.TestCase):
    def test_spawn_exception_returns_none(self):
        with mock.patch("subprocess.run", side_effect=OSError("CreateProcess fail")):
            self.assertIsNone(_ping_ip("10.0.0.1"))

    def test_timeout_returns_none(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ping", 9)):
            self.assertIsNone(_ping_ip("10.0.0.1"))

    def test_ttl_present_returns_true(self):
        out = b"\r\nReply from 10.0.0.1: bytes=32 time=1ms TTL=64\r\n"
        with mock.patch("subprocess.run", return_value=_fake_run_result(out)):
            self.assertTrue(_ping_ip("10.0.0.1"))

    def test_no_ttl_returns_false(self):
        out = b"Request timed out.\r\nRequest timed out.\r\n"
        with mock.patch("subprocess.run", return_value=_fake_run_result(out)):
            self.assertFalse(_ping_ip("10.0.0.1"))

    def test_windows_uses_create_no_window(self):
        with mock.patch("sys.platform", "win32"), mock.patch(
            "subprocess.run", return_value=_fake_run_result(b"TTL=64")
        ) as m:
            _ping_ip("10.0.0.1")
            kwargs = m.call_args.kwargs
            self.assertIn("creationflags", kwargs)
            self.assertEqual(
                kwargs["creationflags"], getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)
            )

    def test_linux_no_creationflags(self):
        with mock.patch("sys.platform", "linux"), mock.patch(
            "subprocess.run", return_value=_fake_run_result(b"TTL=64")
        ) as m:
            _ping_ip("10.0.0.1")
            self.assertNotIn("creationflags", m.call_args.kwargs)


class FakeTerminal:
    def __init__(self):
        self.id = 1
        self.name = "t1"
        self.ip = "10.0.0.1"
        self.status = "online"
        self.online_count = 0
        self.offline_count = 0
        self.last_online_at = None
        self.last_offline_at = None
        self.last_check_at = None


class FakeDB:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


class TestProbeSingleSkip(unittest.TestCase):
    def _run(self):
        return asyncio.run(_probe_single(FakeDB(), FakeTerminal()))

    def test_none_skips_without_touching_state(self):
        t = FakeTerminal()
        db = FakeDB()
        with mock.patch(
            "app.services.business_monitor_service.probe_terminal_online",
            new=mock.AsyncMock(return_value=None),
        ):
            asyncio.run(_probe_single(db, t))
        self.assertEqual(t.offline_count, 0)
        self.assertEqual(t.online_count, 0)
        self.assertEqual(t.status, "online")
        self.assertIsNone(t.last_check_at)
        self.assertEqual(db.commits, 0)
        self.assertEqual(db.added, [])

    def test_false_still_counts_offline(self):
        t = FakeTerminal()
        db = FakeDB()
        with mock.patch(
            "app.services.business_monitor_service.probe_terminal_online",
            new=mock.AsyncMock(return_value=False),
        ):
            asyncio.run(_probe_single(db, t))
        self.assertEqual(t.offline_count, 1)
        self.assertEqual(t.status, "online")  # 1 次未达阈值不判离线
        self.assertIsNotNone(t.last_check_at)
        self.assertEqual(db.commits, 1)

    def test_true_counts_online_and_commits(self):
        t = FakeTerminal()
        db = FakeDB()
        with mock.patch(
            "app.services.business_monitor_service.probe_terminal_online",
            new=mock.AsyncMock(return_value=True),
        ):
            asyncio.run(_probe_single(db, t))
        self.assertEqual(t.online_count, 1)
        self.assertEqual(t.status, "online")
        self.assertEqual(db.commits, 1)

    def test_async_probe_passthrough(self):
        async def _case():
            with mock.patch(
                "app.services.business_monitor_service._ping_ip", return_value=True
            ):
                self.assertTrue(await probe_terminal_online("10.0.0.1"))
                with mock.patch(
                    "app.services.business_monitor_service._ping_ip",
                    return_value=None,
                ):
                    self.assertIsNone(await probe_terminal_online("10.0.0.1"))

        asyncio.run(_case())


if __name__ == "__main__":
    unittest.main(verbosity=2)
