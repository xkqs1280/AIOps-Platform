"""迁移在真实 PostgreSQL 上的执行正确性（存量库升级路径）。

SQLite 不支持 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`，因此这组用例只在
DATABASE_URL 指向 PostgreSQL 时执行（本地默认跳过，CI 用 postgres service 跑）。

设计要点：迁移存在的意义就是「把存量库升到新结构」，所以这里不只测幂等，
更要复现 **老表结构 → 迁移补列 → 种子插入** 的真实升级链路——
2026-09-10 部署到 .108 时，正是这条链路因「种子先于迁移」而使服务启动失败。
"""
import pytest
from sqlalchemy import text

from app.database import create_schema, engine, seed_default_data
from app.migrations import MIGRATIONS, run_migrations

pytestmark = pytest.mark.skipif(
    not engine.dialect.name == "postgresql",
    reason="需要 PostgreSQL（SQLite 不支持 ADD COLUMN IF NOT EXISTS）",
)

# 4.6.0 迁移之前的 alert_rules 结构（无 mode / baseline_sigma / baseline_direction）
LEGACY_ALERT_RULES_DDL = """
CREATE TABLE alert_rules (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(128) UNIQUE NOT NULL,
    metric      VARCHAR(64)  NOT NULL,
    condition   VARCHAR(32)  NOT NULL,
    threshold   DOUBLE PRECISION NOT NULL,
    duration    INTEGER,
    severity    VARCHAR(16)  NOT NULL,
    enabled     BOOLEAN,
    description VARCHAR(512),
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT now()
)
"""


@pytest.fixture(autouse=True)
async def _ensure_tables():
    """迁移是对「已存在的表」做 ALTER，没有表就无从迁移，先建好完整结构。"""
    await create_schema()
    yield


async def test_run_migrations_is_idempotent():
    """迁移必须可重复执行：升级流程会多次调用，第二次应当是 no-op。"""
    first = await run_migrations()
    second = await run_migrations()
    assert second == [], f"第二次执行仍有迁移被应用：{second}"
    assert isinstance(first, list)


async def test_all_migrations_recorded():
    """执行后 schema_migrations 必须包含全部版本号。"""
    await run_migrations()
    async with engine.begin() as conn:
        rows = (await conn.execute(text("SELECT version FROM schema_migrations"))).all()
    applied = {r[0] for r in rows}
    assert set(MIGRATIONS.keys()).issubset(applied)


async def test_alert_enhancement_columns_exist():
    """4.6.0 新增列必须真实落到表结构上。"""
    await run_migrations()
    expected = {
        "alert_rules": {"mode", "baseline_sigma", "baseline_direction"},
        "alerts": {
            "flap_count", "suppressed_by_device_id",
            "suppress_reason", "aggregated_count",
        },
    }
    async with engine.begin() as conn:
        for table, columns in expected.items():
            rows = (await conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
            ), {"t": table})).all()
            actual = {r[0] for r in rows}
            missing = columns - actual
            assert not missing, f"{table} 缺少列：{missing}"


async def test_legacy_db_upgrade_then_seed():
    """存量库升级全链路：老 alert_rules → 迁移补列 → 种子插入必须走通。

    这是 2026-09-10 事故的直接回归：当时种子规则引用了尚未迁移出来的
    `mode` 列，INSERT 报 `column "mode" of relation "alert_rules" does not exist`，
    服务启动失败。同时校验存量规则行为不变、基线种子默认关闭、列的 DB 默认值正确。
    """
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS alert_rules CASCADE"))
        await conn.execute(text(LEGACY_ALERT_RULES_DDL))
        await conn.execute(text(
            "INSERT INTO alert_rules (name, metric, condition, threshold, severity, enabled) "
            "VALUES ('存量规则-CPU', 'cpu_usage', 'gt', 90, 'critical', true)"
        ))
        # 把 4.6.0 记为「未应用」，让它对老表重新执行一次
        await conn.execute(text("DELETE FROM schema_migrations WHERE version = '4.6.0'"))

    try:
        await create_schema()  # 老表已存在：create_all 不会给旧表补列
        applied = await run_migrations()
        assert "4.6.0" in applied, f"4.6.0 未被执行：{applied}"
        await seed_default_data()  # 修复前此步必炸

        async with engine.begin() as conn:
            cols = {r[0] for r in (await conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name='alert_rules'"
            ))).all()}
            assert {"mode", "baseline_sigma", "baseline_direction"} <= cols, f"迁移未补列：{cols}"

            rows = (await conn.execute(text(
                "SELECT name, mode, enabled FROM alert_rules ORDER BY id"
            ))).all()
            dflt = (await conn.execute(text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name='alert_rules' AND column_name='mode'"
            ))).scalar_one()

        info = {r[0]: (r[1], r[2]) for r in rows}
        # 存量规则升级后行为必须完全不变
        assert info["存量规则-CPU"][0] == "threshold"
        # 动态基线种子已插入且默认关闭（需先积累采样再启用）
        baseline_names = [n for n, (m, _e) in info.items() if m == "baseline"]
        assert baseline_names, "动态基线种子规则未插入"
        for n in baseline_names:
            assert info[n][1] is False, f"{n} 应默认关闭"
        # 迁移写入的 DB 默认值（存量默认语义）必须真实存在
        assert dflt and "threshold" in str(dflt), f"mode 列默认值异常：{dflt}"
    finally:
        # 还原为完整结构，避免影响同库的其他用例
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS alert_rules CASCADE"))
        await create_schema()
        await run_migrations()
