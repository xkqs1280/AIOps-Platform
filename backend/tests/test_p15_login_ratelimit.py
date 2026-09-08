# -*- coding: utf-8 -*-
"""P1-5 回归：登录防爆破区分 IP / 账号维度 + TTL + 容量上限。

修复要点：
- 旧实现 `_username_fail` 纯按 username 聚合，任意单 IP 刷 5 次错误密码即可
  无成本锁死 admin（DoS），且进程内 dict 无界膨胀。
- 新实现：单来源暴力由 IP 维度锁定（锁攻击者自己的 IP）；账号维度仅在
  失败总数 >= ACCOUNT_FAIL_MAX(10) 且来源 IP >= ACCOUNT_MIN_SOURCES(3)
  （分布式爆破特征）时才锁定账号；所有状态桶 TTL 过期删除 + 总桶数上限。
"""
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, "backend")

from fastapi import HTTPException  # noqa: E402

import app.services.rate_limit as rl  # noqa: E402


def _req(ip: str):
    return mock.Mock(client=mock.Mock(host=ip))


class TestIpDimension(unittest.TestCase):
    def setUp(self):
        rl.reset_state()

    def test_single_ip_gets_locked_after_fail_max(self):
        # 单 IP 连续失败 FAIL_MAX 次 → 该 IP 被锁（limit_login 抛 429）
        ip = "10.0.0.1"
        for _ in range(rl.FAIL_MAX):
            rl.record_login_failure(ip, "admin")
        with self.assertRaises(HTTPException):
            import asyncio
            asyncio.run(rl.limit_login(_req(ip)))

    def test_single_ip_does_not_lock_account(self):
        # 单源（< 3 个来源 IP）失败再多也不触发账号锁 → 其它 IP 仍可正常登录
        ip = "10.0.0.1"
        for _ in range(rl.FAIL_MAX + 3):  # 8 次失败，仅 1 个来源
            rl.record_login_failure(ip, "admin")
        # 换个干净 IP 登录 admin：不应被账号锁拒绝
        try:
            rl.check_username_locked("admin", "10.0.0.99")
        except HTTPException:
            self.fail("单源暴力不应锁死账号（防 DoS）")


class TestAccountDistributedLock(unittest.TestCase):
    def setUp(self):
        rl.reset_state()

    def test_distributed_bruteforce_locks_account(self):
        # 3 个来源 IP 各失败数/次，凑够 ACCOUNT_FAIL_MAX 且来源 >= 3 → 锁账号
        sources = ["10.0.0.11", "10.0.0.12", "10.0.0.13", "10.0.0.14"]
        per = -(-rl.ACCOUNT_FAIL_MAX // len(sources))  # 每源 ceil(10/4)=3
        for ip in sources:
            for _ in range(per):
                rl.record_login_failure(ip, "Admin")  # 大小写不敏感
        # 任一新来源尝试登录 admin 也应被锁（包括合法用户 IP）
        with self.assertRaises(HTTPException) as ctx:
            rl.check_username_locked("admin", "10.0.0.99")
        self.assertEqual(ctx.exception.status_code, 429)

    def test_distinct_username_isolated(self):
        # 账号 A 被分布式锁，不影响账号 B
        for ip in ("10.0.0.21", "10.0.0.22", "10.0.0.23"):
            for _ in range(4):
                rl.record_login_failure(ip, "victim")
        with self.assertRaises(HTTPException):
            rl.check_username_locked("victim", "10.0.0.99")  # 4*3=12 >=10 且 3 源 → 抛
        try:
            rl.check_username_locked("other", "10.0.0.99")  # 不抛
        except HTTPException:
            self.fail("不同账号不应互相影响")


class TestTTL(unittest.TestCase):
    def setUp(self):
        rl.reset_state()

    def test_account_lock_expires_after_window(self):
        # 伪造时间推进 ACCOUNT_WINDOW 后，失败记录过期 → 账号锁自动解除
        base = time.monotonic()
        with mock.patch.object(rl, "monotonic", side_effect=lambda: base):
            for ip in ("10.0.1.1", "10.0.1.2", "10.0.1.3"):
                for _ in range(4):
                    rl.record_login_failure(ip, "admin")
            with self.assertRaises(HTTPException):
                rl.check_username_locked("admin", "10.0.1.99")
        # 窗口过后（> ACCOUNT_WINDOW 秒）
        later = base + rl.ACCOUNT_WINDOW + 1
        with mock.patch.object(rl, "monotonic", side_effect=lambda: later):
            try:
                rl.check_username_locked("admin", "10.0.1.99")
            except HTTPException:
                self.fail("过期后账号锁应自动解除")

    def test_ip_lock_expires_after_window(self):
        base = time.monotonic()
        with mock.patch.object(rl, "monotonic", side_effect=lambda: base):
            for _ in range(rl.FAIL_MAX):
                rl.record_login_failure("10.0.2.1", "admin")
            with self.assertRaises(HTTPException):
                import asyncio
                asyncio.run(rl.limit_login(_req("10.0.2.1")))
        later = base + rl.FAIL_WINDOW + 1
        with mock.patch.object(rl, "monotonic", side_effect=lambda: later):
            try:
                import asyncio
                asyncio.run(rl.limit_login(_req("10.0.2.1")))
            except HTTPException:
                self.fail("过期后 IP 锁应自动解除")


class TestBoundedMemory(unittest.TestCase):
    def setUp(self):
        rl.reset_state()

    def test_username_storage_capped(self):
        # 海量随机 username 只产生失败 → dict 不超过 MAX_BUCKETS
        with mock.patch.object(rl, "MAX_BUCKETS", 500):
            for i in range(1000):
                rl.record_login_failure(f"10.0.3.{i % 250 + 1}", f"user{i}")
        self.assertLessEqual(len(rl._username_fail), 500)
        self.assertLessEqual(len(rl._failures), 500)

    def test_ingest_storage_capped(self):
        import asyncio
        with mock.patch.object(rl, "MAX_BUCKETS", 500):
            for i in range(1000):
                asyncio.run(rl.limit_ingest(_req(f"10.0.4.{i % 250 + 1}")))
        self.assertLessEqual(len(rl._requests), 500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
