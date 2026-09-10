"""pytest 公共装置。

设计要点：
  - 测试默认跑在 **SQLite 内存库**上，无需外部 PostgreSQL 即可执行，CI 与本地一致；
  - 模型里用到的 PostgreSQL 专属类型/行为（JSONB、BigInteger 自增主键）在 SQLite 上
    不可用，这里用 `@compiles` 打补丁做等价降级（JSONB→JSON、BIGINT→INTEGER）；
  - DATABASE_URL 必须在导入 app.* 之前设置：app.database 在导入期就会按 URL 建引擎。
"""
import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# ---- 必须在导入 app.* 之前完成环境准备 ----
# 用 setdefault：CI 里可通过 DATABASE_URL 切到 PostgreSQL service 跑真实方言的用例
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", "")
os.environ.setdefault("BOOTSTRAP_ADMIN_USERNAME", "admin")
os.environ.setdefault("BOOTSTRAP_ADMIN_PASSWORD", "Admin@123456")
os.environ.setdefault("LICENSE_ENABLED", "false")
os.environ.setdefault("AI_BASE_URL", "http://127.0.0.1:11434/v1")

from sqlalchemy import BigInteger  # noqa: E402
from sqlalchemy.dialects.postgresql import ARRAY, JSONB  # noqa: E402
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402


# SQLite 没有 JSONB / ARRAY 的编译器，直接给类型编译器挂上等价的渲染方法
# （比 @compiles 更可靠：@compiles 在方言缺少 visit_XXX 时不会兜底）。
SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore[attr-defined]
SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "JSON"  # type: ignore[attr-defined]


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(type_, compiler, **kw):  # pragma: no cover - 方言适配
    # SQLite 只有 INTEGER PRIMARY KEY 才是 rowid 别名（可自增），BIGINT 不行
    return "INTEGER"


@pytest.fixture(autouse=True)
def _reset_process_state():
    """清空各服务模块的进程级内存状态，保证用例互不干扰。

    告警引擎的状态库、收敛模块的抖动/风暴窗口都是模块级字典，
    邮件/通知的防轰炸窗口也是——不清理会串味。
    """
    from app.services import alert_rule_engine, alert_suppressor, mail_service, notify_service

    yield

    alert_rule_engine._state.clear()
    alert_rule_engine._uptime_prev.clear()
    alert_rule_engine._if_prev_status.clear()
    alert_rule_engine._if_down_since.clear()
    alert_rule_engine._if_err_prev.clear()
    alert_rule_engine._if_err_since.clear()
    alert_suppressor._resolve_history.clear()
    alert_suppressor._alert_history.clear()
    alert_suppressor._flap_counts.clear()
    mail_service._last_sent.clear()
    notify_service._last_sent.clear()


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def db():
    """每个用例一套干净的表结构（内存库 + StaticPool 共享同一连接）。"""
    from app.database import Base, async_session, engine
    import app.models  # noqa: F401 — 触发全部模型注册到 metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with async_session() as session:
            yield session
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
def device_factory(db):
    """创建设备的便捷工厂（返回的是已落库、带 id 的 Device 实例）。"""
    from app.models.device import Device

    created: list = []

    async def _make(name: str = "dev", ip: str = "10.0.0.1", status: str = "online", **kw):
        d = Device(name=name, ip=ip, status=status, **kw)
        db.add(d)
        await db.commit()
        await db.refresh(d)
        created.append(d)
        return d

    return _make


@pytest.fixture
def rule_factory(db):
    """创建告警规则的便捷工厂。"""
    from app.models.alert import AlertRule

    async def _make(name: str = "rule", metric: str = "cpu_usage",
                    condition: str = "gt", threshold: float = 90.0,
                    duration: int = 0, severity: str = "critical",
                    mode: str = "threshold", **kw):
        r = AlertRule(
            name=name, metric=metric, condition=condition, threshold=threshold,
            duration=duration, severity=severity, enabled=True, mode=mode, **kw,
        )
        db.add(r)
        await db.commit()
        await db.refresh(r)
        return r

    return _make
