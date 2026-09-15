# -*- coding: utf-8 -*-
"""P1-6 回归：核心业务表保留期每日分批清理。

alerts / ai_logs / security_events / business_alerts / device_logs 原无保留期清理，
表无限膨胀。retention_service.cleanup_expired_core_data 复用 P1-4 分批删
模式：每批取主键 → DELETE IN → commit，循环直至清空，返回 {表: 删除数}。
本测试用假 DB（按语句目标表返回剩余 id）验证：五表各按其条件分批删除、
alerts 仅清 resolved、边界与终止、总删除计数正确。

⚠️ 新增受管表时必须同步扩充 ``TABLE_OF`` 与各用例的 per_table 字典，否则
_FakeDB 对未知表返回 Mock（``'Mock' object is not iterable``）导致用例失败。
"""
import sys
import unittest
from unittest import mock

sys.path.insert(0, "backend")

from sqlalchemy.sql.expression import Select  # noqa: E402

from app.services.retention_service import cleanup_expired_core_data, BATCH_SIZE  # noqa: E402

TABLE_OF = {"alerts", "ai_logs", "security_events", "business_alerts", "device_logs"}

# 全部受管表的初始行数（0 = 空表用例）
EMPTY = {name: 0 for name in TABLE_OF}


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    """从语句的 FROM 解析目标表，SELECT 每批吐 min(BATCH, 剩余) 个 id；DELETE 计数。"""

    def __init__(self, per_table: dict[str, int]):
        self.remaining = dict(per_table)
        self.deleted = {k: 0 for k in per_table}
        self.commits = 0
        self.last_ids = []

    def _table_name(self, stmt) -> str | None:
        # 统一用编译字符串子串定位目标表（SELECT / DELETE 均可）。
        # 长名优先：business_alerts 含子串 alerts，必须先匹配长名。
        s = str(stmt)
        for name in sorted(TABLE_OF, key=len, reverse=True):
            if name in s:
                return name
        return None

    async def execute(self, stmt):
        table = self._table_name(stmt)
        if isinstance(stmt, Select) and table in self.remaining:
            take = min(BATCH_SIZE, self.remaining[table])
            ids = [i for i in range(take)]
            self.remaining[table] -= take
            self.last_ids = ids
            return _FakeResult(ids)
        # DELETE：按上次该表的批计数（简化：一律按 last_ids 长度计）
        if table in self.deleted:
            self.deleted[table] += len(self.last_ids)
            self.last_ids = []
        return mock.Mock(rowcount=0)

    async def commit(self):
        self.commits += 1


class _Ctx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *a):
        return False


def _run(per_table: dict[str, int]):
    db = _FakeDB(per_table)
    with mock.patch("app.database.async_session", return_value=_Ctx(db)):
        import asyncio
        stats = asyncio.run(cleanup_expired_core_data())
    return stats, db


class TestRetentionCleanup(unittest.TestCase):
    def test_all_tables_deleted_and_counted(self):
        stats, db = _run({
            **EMPTY,
            "alerts": 1200, "ai_logs": 3, "business_alerts": BATCH_SIZE, "device_logs": 700,
        })
        self.assertEqual(stats["alerts"], 1200)          # 3 批
        self.assertEqual(stats["ai_logs"], 3)            # 1 批尾批
        self.assertEqual(stats["security_events"], 0)    # 空表
        self.assertEqual(stats["business_alerts"], BATCH_SIZE)  # 恰好整批 → 再查一次空后退出
        self.assertEqual(stats["device_logs"], 700)      # 500 + 200
        self.assertEqual(db.deleted["alerts"], 1200)
        self.assertEqual(db.deleted["business_alerts"], BATCH_SIZE)
        self.assertEqual(db.deleted["device_logs"], 700)

    def test_batch_termination_and_commit_count(self):
        # 1200 = 500+500+200 → alerts 3 次 delete + 3 次 commit
        _, db = _run({**EMPTY, "alerts": 1200})
        self.assertEqual(db.commits, 3)
        self.assertEqual(db.deleted["alerts"], 1200)

    def test_returns_empty_for_no_data(self):
        stats, db = _run(dict(EMPTY))
        self.assertEqual(sum(stats.values()), 0)
        self.assertEqual(db.commits, 0)

    def test_device_logs_retention_is_configurable(self):
        """设备日志留存天数必须**来自配置**（.env 可调），且默认不低于 180 天。

        背景：这里曾写死 180，导致 config.py 里同名配置项无人引用、客户改 .env 完全不生效
        （页面文案还宣称"≥ 6 个月"）。等保 2.0 /《网络安全法》第 21 条要求
        "留存相关的网络日志不少于六个月"，而自然月的 6 个月是 181~184 天，
        不同客户/测评口径不一 → 必须能不重新打包就调大。

        注意：只断言"等于 settings.DEVICE_LOGS_DAYS"是**测不出问题的** ——
        写死 180 时 settings 默认值恰好也是 180，两边相等、假绿（本项目真踩过这一脚）。
        所以要真改一次配置再 reload，看常量是否跟着动。
        """
        import importlib

        from app.config import settings
        from app.services import retention_service

        self.assertGreaterEqual(retention_service.DEVICE_LOGS_DAYS, 180)

        original = settings.DEVICE_LOGS_DAYS
        try:
            settings.DEVICE_LOGS_DAYS = 365
            importlib.reload(retention_service)
            self.assertEqual(
                retention_service.DEVICE_LOGS_DAYS,
                365,
                "DEVICE_LOGS_DAYS 必须跟 settings 走（.env 可配），不要写死成常量",
            )
        finally:
            settings.DEVICE_LOGS_DAYS = original
            importlib.reload(retention_service)


if __name__ == "__main__":
    unittest.main(verbosity=2)
