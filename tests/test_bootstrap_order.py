"""回归：数据库启动初始化顺序必须为 create_schema → run_migrations → seed_default_data。

背景（2026-09-10 .108 部署实测踩坑）：新增的动态基线种子规则引用了
alert_rules 的 mode/baseline_sigma/baseline_direction 列，而这些列在存量库上
要靠迁移补齐。原实现把「建表 + 写种子」绑在 init_db() 里、且在迁移之前执行，
导致存量库升级时 INSERT ... mode ... 报
`column "mode" of relation "alert_rules" does not exist` → 服务启动失败。

因此顺序契约必须被测试锁死：种子永远在迁移之后。
"""
import app.main as main
import app.migrations as migrations


async def test_bootstrap_order_is_schema_then_migration_then_seed(monkeypatch):
    calls = []

    async def fake_create_schema():
        calls.append("create_schema")

    async def fake_run_migrations():
        calls.append("run_migrations")
        return ["4.6.0"]

    async def fake_seed_default_data():
        calls.append("seed_default_data")

    monkeypatch.setattr(main, "create_schema", fake_create_schema)
    monkeypatch.setattr(main, "seed_default_data", fake_seed_default_data)
    monkeypatch.setattr(migrations, "run_migrations", fake_run_migrations)

    await main._bootstrap_database()

    assert calls == ["create_schema", "run_migrations", "seed_default_data"], (
        f"启动初始化顺序错误：{calls}；种子数据必须在迁移之后执行"
    )


async def test_bootstrap_reraises_migration_failure(monkeypatch):
    """迁移失败必须抛出让启动中断（否则会带着半迁移的库对外服务）。"""
    import pytest

    async def fake_create_schema():
        pass

    async def boom_migrations():
        raise RuntimeError("migration boom")

    monkeypatch.setattr(main, "create_schema", fake_create_schema)
    monkeypatch.setattr(migrations, "run_migrations", boom_migrations)

    with pytest.raises(RuntimeError):
        await main._bootstrap_database()


async def test_seed_failure_does_not_block_startup(monkeypatch):
    """种子失败只记日志、不阻断启动（避免非关键数据问题拖垮服务）。"""
    async def fake_create_schema():
        pass

    async def fake_run_migrations():
        return []

    async def boom_seed():
        raise RuntimeError("seed boom")

    monkeypatch.setattr(main, "create_schema", fake_create_schema)
    monkeypatch.setattr(migrations, "run_migrations", fake_run_migrations)
    # _bootstrap_database 内部引用的是 main 模块导入的名字
    monkeypatch.setattr(main, "seed_default_data", boom_seed)

    await main._bootstrap_database()  # 不应抛出
