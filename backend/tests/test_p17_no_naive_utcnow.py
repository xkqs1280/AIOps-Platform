# -*- coding: utf-8 -*-
"""P1-7 静态守卫：后端服务目录不得再出现 naive `datetime.utcnow()`。

naive UTC 写入 timestamptz 列 / 与其比较时依赖 PG 会话时区（测试服常 +08），
导致 7/30 天窗口与告警统计偏移。修复后统一 `datetime.now(timezone.utc)`。
此测试扫描源码防止回退。
"""
import os
import re
import sys
import unittest

sys.path.insert(0, "backend")

BACKEND_APP = os.path.join(os.path.dirname(__file__), "..", "app")
UTCNOW_RE = re.compile(r"datetime\.utcnow\(|utcnow\(")
# 允许出现 utcnow 的说明性字符串所在文件（注释/文档示例）
SKIP = {"__pycache__"}


def _iter_py_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


class TestNoNaiveUtcNow(unittest.TestCase):
    def test_no_utcnow_anywhere_in_app(self):
        offenders = []
        for path in _iter_py_files(BACKEND_APP):
            with open(path, encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    if UTCNOW_RE.search(line):
                        offenders.append(f"{os.path.relpath(path)}:{lineno}")
        self.assertEqual(offenders, [], f"发现 naive utcnow 使用：{offenders}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
