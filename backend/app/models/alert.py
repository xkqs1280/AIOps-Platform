from datetime import datetime

from sqlalchemy import BigInteger, String, DateTime, Text, ForeignKey, func, Index, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    condition: Mapped[str] = mapped_column(String(32), nullable=False)
    threshold: Mapped[float] = mapped_column(nullable=False)
    duration: Mapped[int] = mapped_column(default=300)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True)
    description: Mapped[str | None] = mapped_column(String(512))
    # ---- 动态基线判定（mode="baseline" 时生效，threshold 字段作为兜底上限）----
    # mode: "threshold"=静态阈值（默认，保持存量行为）/ "baseline"=同时段动态基线
    mode: Mapped[str] = mapped_column(String(16), default="threshold", nullable=False)
    # 偏离倍数：|z-score| 超过该值判定为异常（默认 3.0）
    baseline_sigma: Mapped[float] = mapped_column(default=3.0, nullable=False)
    # 偏离方向：upper=只看高于基线 / lower=只看低于基线 / both=双向
    baseline_direction: Mapped[str] = mapped_column(String(8), default="upper", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        # 健康检测/告警引擎按设备+规则查 active 告警、SSE 按 id 增量、恢复按 resolved_at
        Index("ix_alerts_device_rule_status", "device_id", "rule_name", "status"),
        Index("ix_alerts_status", "status"),
        Index("ix_alerts_triggered_at", "triggered_at"),
        Index("ix_alerts_resolved_at", "resolved_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("devices.id"), nullable=False)
    rule_name: Mapped[str] = mapped_column(String(128))
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text)
    # status: active=活动 / resolved=已恢复 / suppressed=被抑制（上游离线或告警风暴）
    status: Mapped[str] = mapped_column(String(16), default="active")
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ---- 收敛信息（P0 告警收敛）----
    # 抖动次数：该告警在抖动窗口内被反复触发/恢复的次数（>0 表示链路不稳定）
    flap_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 被哪个上游设备的状态所抑制（NULL=未被依赖抑制）
    suppressed_by_device_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    suppress_reason: Mapped[str | None] = mapped_column(String(255))
    # 风暴聚合：该告警代表了多少条被折叠掉的同类告警
    aggregated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    device = relationship("Device", back_populates="alerts", lazy="selectin")
