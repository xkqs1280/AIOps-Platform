from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text
from sqlalchemy.pool import StaticPool

from app.config import settings

# 引擎参数按方言区分：
#  - PostgreSQL（生产）：连接池按并发采集/SSE 规模配置；
#  - SQLite（单元测试）：不支持 pool_size/max_overflow，且内存库需 StaticPool
#    才能在多个会话间共享同一份数据。
if settings.DATABASE_URL.startswith("postgresql"):
    engine = create_async_engine(
        settings.DATABASE_URL, echo=False, pool_size=20, max_overflow=10
    )
else:
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


# Alias used by P3 routers and services
get_session = get_db


DEFAULT_ALERT_RULES = [
    # (name, metric, condition, threshold, duration, severity, description)
    # 注意：condition 必须与 alert_rule_engine._check_condition 一致（gt/gte/lt/lte/eq/ne），
    # 不要用 >、== 等符号，否则引擎永远不会评估触发该规则。
    ("CPU 使用率过高", "cpu_usage", "gt", 90, 300, "critical", "设备 CPU 使用率连续 5 分钟超过 90%"),
    ("CPU 使用率偏高", "cpu_usage", "gt", 80, 600, "warning", "设备 CPU 使用率连续 10 分钟超过 80%"),
    ("内存使用率过高", "memory_usage", "gt", 90, 300, "critical", "设备内存使用率连续 5 分钟超过 90%"),
    ("内存使用率偏高", "memory_usage", "gt", 80, 600, "warning", "设备内存使用率连续 10 分钟超过 80%"),
    ("设备温度过高", "temperature", "gt", 70, 180, "critical", "设备温度连续 3 分钟超过 70℃"),
    ("设备离线", "online_status", "eq", 0, 180, "critical", "设备连续 3 次健康检查失败，判定离线"),
    ("接口带宽利用率过高", "bandwidth_usage", "gt", 85, 600, "warning", "接口带宽利用率连续 10 分钟超过 85%"),
    ("接口状态异常", "interface_status", "eq", 0, 60, "warning", "接口 down 状态"),
    ("配置文件变更", "config_change", "eq", 1, 0, "warning", "检测到设备配置发生变更"),
    ("日志异常事件", "security_event", "gt", 0, 0, "info", "产生安全事件日志"),
    # 接口错包速率（IF-MIB 计数器两次采样差值/秒，需设备支持 IF-MIB）
    ("入接口错误率高", "if_in_errors", "gt", 1, 60, "major", "接口入向错包速率 >1 个/秒，持续 60 秒判定错误率高"),
    ("出接口错误率高", "if_out_errors", "gt", 1, 60, "major", "接口出向错包速率 >1 个/秒，持续 60 秒判定错误率高"),
    # ---- 动态基线模式（mode=baseline）种子 ----
    # threshold 在基线模式下是「绝对下限」，防止 1%→3% 这类小值大偏离被判为异常。
    # 默认 **不启用**：需要先积累至少 MIN_SAMPLES 条同时段采样，基线才会产出；
    # 用户在告警规则页确认基线已生成后再开启即可。
    ("CPU 偏离同时段基线", "cpu_usage", "gt", 30, 600, "warning",
     "CPU 利用率显著偏离同时段历史基线（动态基线模式；数值为绝对下限 30%，需先积累基线数据再启用）"),
    ("内存偏离同时段基线", "memory_usage", "gt", 40, 600, "warning",
     "内存利用率显著偏离同时段历史基线（动态基线模式；数值为绝对下限 40%，需先积累基线数据再启用）"),
]

# 上述种子里以「动态基线」模式落库、且默认不启用的规则名
BASELINE_SEED_RULES = {"CPU 偏离同时段基线", "内存偏离同时段基线"}


async def create_schema():
    """建新表并补索引。

    注意：create_all **不会修改已存在表的结构**，因此本函数不负责给旧表加列——
    旧表结构变更必须走 migrations（见 app/migrations.py）。服务启动顺序固定为
    create_schema → run_migrations → seed_default_data：种子规则可能引用迁移
    新增的列，必须等迁移补齐列之后再插入，否则 INSERT 会因列不存在而失败。
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # 已有表补建索引（create_all 不修改已存在的表，此处幂等补建）
    _INDEX_SQL = [
        "CREATE INDEX IF NOT EXISTS ix_alerts_device_rule_status ON alerts (device_id, rule_name, status)",
        "CREATE INDEX IF NOT EXISTS ix_alerts_status ON alerts (status)",
        "CREATE INDEX IF NOT EXISTS ix_alerts_triggered_at ON alerts (triggered_at)",
        "CREATE INDEX IF NOT EXISTS ix_alerts_resolved_at ON alerts (resolved_at)",
        "CREATE INDEX IF NOT EXISTS ix_config_backups_device_id ON config_backups (device_id)",
        "CREATE INDEX IF NOT EXISTS ix_config_backups_created_at ON config_backups (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_config_backups_status ON config_backups (status)",
        "CREATE INDEX IF NOT EXISTS ix_devices_ip ON devices (ip)",
    ]
    async with engine.begin() as conn:
        for stmt in _INDEX_SQL:
            try:
                await conn.execute(text(stmt))
            except Exception:
                # 表可能尚不存在（全新部署由 create_all 已建），忽略即可
                pass
async def seed_default_data():
    """幂等写入默认告警规则：按 name 逐条检查，缺失才补（升级后新种子也会自动补上，
    不覆盖用户已修改的规则）。condition 必须为引擎格式（gt/eq/...）。

    引用 alert_rules 的 mode/baseline_sigma/baseline_direction 列，因此必须在
    run_migrations() 之后调用（存量库这些列要靠迁移补齐）。
    """
    from sqlalchemy import select
    from app.models.alert import AlertRule

    async with async_session() as session:
        for name, metric, condition, threshold, duration, severity, desc in DEFAULT_ALERT_RULES:
            exists = (await session.execute(
                select(AlertRule.id).where(AlertRule.name == name)
            )).scalar_one_or_none()
            if exists is None:
                is_baseline = name in BASELINE_SEED_RULES
                session.add(AlertRule(
                    name=name, metric=metric, condition=condition,
                    threshold=threshold, duration=duration,
                    severity=severity, description=desc,
                    # 动态基线规则需要先积累采样数据，默认关闭由用户确认后启用
                    enabled=not is_baseline,
                    mode="baseline" if is_baseline else "threshold",
                ))
        await session.commit()


async def init_db():
    """完整初始化（建表 + 补索引 + 种子数据）。

    仅供 seed_data.py 等一次性脚本使用；**服务启动路径请勿用它**，应显式按
    create_schema → run_migrations → seed_default_data 的顺序执行（见 main.py）。
    """
    await create_schema()
    await seed_default_data()
