"""指标时序表 - 存储设备 CPU/内存/温度的真实采集历史，供趋势图与健康评分使用。"""
from datetime import datetime

from sqlalchemy import String, DateTime, Float, Integer, func, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MetricRecord(Base):
    __tablename__ = "metric_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    # metric_type: cpu / memory / temperature
    metric_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # 百分比值（cpu%/memory%/temperature°C），不可用时为 None
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 原始值（如内存字节数），便于校准与审计
    raw_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    __table_args__ = (
        Index("ix_metric_device_type_time", "device_id", "metric_type", "recorded_at"),
    )
