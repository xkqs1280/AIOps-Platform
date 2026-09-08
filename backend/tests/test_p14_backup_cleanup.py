# -*- coding: utf-8 -*-
"""P1-4 回归：cleanup_old_backups 分批删除。

原实现一次性 SELECT 全表 + 逐条 ORM delete，历史记录多时会把整表载入内存
（config_content 为超大 Text），且单事务过长。改造后每批取主键（默认 500）
→ DELETE ... WHERE id IN (...)→ 提交，循环直至无剩余。
本测试用假 DB 验证：分批次数、删除总数、提交次数、边界（0 / 满批 / 尾批）。
"""
import asyncio
import sys
import unittest
from unittest import mock

sys.path.insert(0, "backend")

from sqlalchemy.sql.expression import Select  # noqa: E402

from app.services.backup_service import cleanup_old_backups  # noqa: E402

BATCH = 500


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    """模拟 async_session：SELECT 每次吐 min(BATCH, 剩余) 个 id；DELETE 累加计数。"""

    def __init__(self, total):
        self.remaining = total
        self.next_id = 0
        self.deleted = 0
        self.commits = 0
        self.last_ids = []

    async def execute(self, stmt):
        if isinstance(stmt, Select):
            take = min(BATCH, self.remaining)
            ids = [self.next_id + i for i in range(take)]
            self.next_id += take
            self.remaining -= take
            self.last_ids = ids
            return _FakeResult([(i,) for i in ids])
        # DELETE 语句
        self.deleted += len(self.last_ids)
        return mock.Mock(rowcount=len(self.last_ids))

    async def commit(self):
        self.commits += 1


class _Ctx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *a):
        return False


def _run(total):
    db = _FakeDB(total)
    with mock.patch("app.database.async_session", return_value=_Ctx(db)):
        n = asyncio.run(cleanup_old_backups())
    return n, db


class TestCleanupBatch(unittest.TestCase):
    def test_zero_records_no_delete(self):
        n, db = _run(0)
        self.assertEqual(n, 0)
        self.assertEqual(db.deleted, 0)
        self.assertEqual(db.commits, 0)

    def test_less_than_one_batch_single_delete(self):
        n, db = _run(30)
        self.assertEqual(n, 30)
        self.assertEqual(db.deleted, 30)
        self.assertEqual(db.commits, 1)  # 尾批 < BATCH → 循环一次即退出

    def test_exact_batch_boundary(self):
        n, db = _run(500)
        self.assertEqual(n, 500)
        self.assertEqual(db.deleted, 500)
        self.assertEqual(db.commits, 1)  # len == BATCH → 还会再查一次，但剩余为 0 退出
        # 第二次 select 返回空 → while 退出，不再 delete/commit

    def test_multi_batch_terminates(self):
        n, db = _run(1234)
        self.assertEqual(n, 1234)
        self.assertEqual(db.deleted, 1234)
        # 500 + 500 + 234 → 3 次 DELETE + 3 次 commit；尾批后退出
        self.assertEqual(db.commits, 3)

    def test_large_volume_bounded_memory(self):
        # 模拟 50 万条历史记录：应分批 1000 次而非全表载入
        n, db = _run(500_000)
        self.assertEqual(n, 500_000)
        self.assertEqual(db.commits, 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
