"""核心业务表保留期清理（P1-6）。

alerts / ai_logs / security_events / business_alerts 原无保留期清理，长期运行
会导致表无限膨胀。本模块提供每日分批清理：
- alerts：仅删除已 resolved 且超过 ALERTS_RESOLVED_DAYS(90) 天的记录（active 告警保留）；
- ai_logs / business_alerts：删除超过对应保留天数的记录；
- security_events：按事件时间 timestamp 删除超过保留天数的记录。

与 backup_service.cleanup_old_backups 同一批删模式：每批取主键 →
DELETE ... WHERE id IN (...) → 提交，避免全表载入内存与超长事务。
"""
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, delete

from app.models.alert import Alert
from app.models.ai import AiLog
from app.models.p3_security import SecurityEvent
from app.models.business_monitor import BusinessAlert

logger = logging.getLogger(__name__)

ALERTS_RESOLVED_DAYS = 90      # 已恢复告警保留 90 天
AI_LOGS_DAYS = 180             # AI 调用日志保留 180 天
SECURITY_EVENTS_DAYS = 180     # 安全事件保留 180 天
BUSINESS_ALERTS_DAYS = 180     # 业务监控告警保留 180 天
BATCH_SIZE = 500


async def _batch_delete(db, model, condition, batch_size: int = BATCH_SIZE) -> int:
    """按 condition 分批删除 model 记录，返回删除条数。"""
    total = 0
    while True:
        id_rows = await db.execute(
            select(model.id).where(condition).order_by(model.id).limit(batch_size)
        )
        ids = list(id_rows.scalars().all())
        if not ids:
            break
        await db.execute(delete(model).where(model.id.in_(ids)))
        await db.commit()
        total += len(ids)
        if len(ids) < batch_size:
            break
    return total


async def cleanup_expired_core_data(
    alerts_resolved_days: int = ALERTS_RESOLVED_DAYS,
    ai_logs_days: int = AI_LOGS_DAYS,
    security_events_days: int = SECURITY_EVENTS_DAYS,
    business_alerts_days: int = BUSINESS_ALERTS_DAYS,
    batch_size: int = BATCH_SIZE,
) -> dict[str, int]:
    """清理各核心业务表的过期记录，返回 {表名: 删除条数}。"""
    from app.database import async_session

    now = datetime.now(timezone.utc)
    stats: dict[str, int] = {}
    async with async_session() as db:
        # 1) 已恢复告警：resolved 超期
        stats["alerts"] = await _batch_delete(
            db, Alert,
            (Alert.status == "resolved") & (Alert.resolved_at < now - timedelta(days=alerts_resolved_days)),
            batch_size,
        )
        # 2) AI 调用日志
        stats["ai_logs"] = await _batch_delete(
            db, AiLog,
            AiLog.created_at < now - timedelta(days=ai_logs_days),
            batch_size,
        )
        # 3) 安全事件（按事件发生时间 timestamp 计保留期）
        stats["security_events"] = await _batch_delete(
            db, SecurityEvent,
            SecurityEvent.timestamp < now - timedelta(days=security_events_days),
            batch_size,
        )
        # 4) 业务监控告警
        stats["business_alerts"] = await _batch_delete(
            db, BusinessAlert,
            BusinessAlert.created_at < now - timedelta(days=business_alerts_days),
            batch_size,
        )
    total = sum(stats.values())
    if total:
        logger.info("Core data retention cleanup: %s", stats)
    return stats
