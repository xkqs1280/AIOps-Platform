# -*- coding: utf-8 -*-
"""数据库结构迁移框架（升级时自动执行，幂等可重复运行）

设计：
  - 迁移记录表 schema_migrations(version, applied_at)，已应用的版本自动跳过；
  - MIGRATIONS 按版本号升序组织，每个版本是一组 SQL 语句（事务内执行）；
  - init_db() 只负责建新表/补索引（不修改旧表），需要变更旧表结构的
    一律走本迁移框架，保证升级后旧数据完整保留；
  - 每个迁移包在事务里执行，失败即回滚该版本并抛错（由升级流程捕获，
    触发整包回滚）。
"""
import logging

from sqlalchemy import text

from app.database import engine

logger = logging.getLogger(__name__)

# 迁移字典：{目标版本: [SQL, ...]}，按 parse_version 升序执行。
# 新增迁移时在末尾追加新版本键，不要修改已发布的版本键内容（线上已执行过）。
MIGRATIONS: dict[str, list[str]] = {
    # 示例（4.1.0 以后有表结构变更时追加）：
    # "4.1.0": [
    #     "ALTER TABLE devices ADD COLUMN IF NOT EXISTS vendor VARCHAR(64)",
    #     "CREATE INDEX IF NOT EXISTS ix_devices_vendor ON devices(vendor)",
    # ],
}

_MIGRATION_ORDER = sorted(MIGRATIONS.keys())


async def _ensure_migration_table():
    async with engine.begin() as conn:
        await conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    VARCHAR(32) PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL DEFAULT now()
            )
            """
        ))


async def _applied_versions() -> set[str]:
    async with engine.begin() as conn:
        rows = await conn.execute(text("SELECT version FROM schema_migrations"))
        return {r[0] for r in rows}


async def run_migrations() -> list[str]:
    """执行所有未应用的迁移，返回本次应用的版本列表。幂等。"""
    await _ensure_migration_table()
    applied = await _applied_versions()
    executed: list[str] = []
    for version in _MIGRATION_ORDER:
        if version in applied:
            continue
        statements = MIGRATIONS[version]
        try:
            async with engine.begin() as conn:
                for sql in statements:
                    await conn.execute(text(sql))
                await conn.execute(
                    text("INSERT INTO schema_migrations (version) VALUES (:v)"),
                    {"v": version},
                )
        except Exception as e:
            logger.error("Migration %s failed, rolling back version: %s", version, e)
            raise
        executed.append(version)
        logger.info("Migration applied: %s", version)
    return executed
