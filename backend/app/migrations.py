# -*- coding: utf-8 -*-
"""数据库结构迁移框架（升级时自动执行，幂等可重复运行）

设计：
  - 迁移记录表 schema_migrations(version, applied_at)，已应用的版本自动跳过；
  - MIGRATIONS 按版本号升序组织，每个版本是一组 SQL 语句（事务内执行）；
  - create_schema() 只负责建新表/补索引（不修改旧表），需要变更旧表结构的
    一律走本迁移框架，保证升级后旧数据完整保留；
  - 启动顺序固定为 create_schema → run_migrations → seed_default_data：
    种子数据可能引用迁移新增的列，必须先迁移后种子（见 main.py::_bootstrap_database）；
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
    # 4.3.4：巡检结果行唯一约束。先清理历史重复行（保留 id 最小的那一行），
    # 再建唯一索引，防止并发/超时路径对同一设备重复插入结果导致 retry 500。
    "4.3.4": [
        """
        DELETE FROM inspection_device_results a
        USING inspection_device_results b
        WHERE a.id < b.id AND a.task_id = b.task_id AND a.device_id = b.device_id
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_inspection_result_task_device ON inspection_device_results(task_id, device_id)",
    ],
    # 4.3.5：授权模式调整（首次部署自动激活 3 个月试用 + 时间回拨检测）。
    #  - source：授权来源（manual=厂商激活码 / auto=自动试用），存量行默认 manual；
    #  - last_seen_at：时间回拨检测锚点（历史观测最大时间），存量行置当前时间。
    "4.3.5": [
        "ALTER TABLE license_info ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'manual'",
        "ALTER TABLE license_info ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP WITH TIME ZONE",
        "UPDATE license_info SET last_seen_at = COALESCE(last_seen_at, activated_at) WHERE last_seen_at IS NULL",
    ],
    # 4.6.0：告警能力增强三件套（动态基线 / 告警收敛 / 多维通知）。
    #  - alert_rules 增加动态基线判定字段：mode=baseline 时按同时段历史基线偏离度判定，
    #    存量规则一律 threshold，行为完全不变；
    #  - alerts 增加收敛字段：抖动次数、被依赖抑制的来源设备与原因、风暴折叠条数；
    #  - status 新增取值 'suppressed'（被上游离线或告警风暴抑制），不计入活跃统计。
    #  新表 device_dependencies / notify_channels 由 init_db() 的 create_all 负责创建。
    "4.6.0": [
        "ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS mode VARCHAR(16) NOT NULL DEFAULT 'threshold'",
        "ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS baseline_sigma DOUBLE PRECISION NOT NULL DEFAULT 3.0",
        "ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS baseline_direction VARCHAR(8) NOT NULL DEFAULT 'upper'",
        "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS flap_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS suppressed_by_device_id BIGINT",
        "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS suppress_reason VARCHAR(255)",
        "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS aggregated_count INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS ix_alerts_suppressed_by ON alerts (suppressed_by_device_id)",
    ],
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
