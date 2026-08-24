from datetime import datetime

from sqlalchemy import String, DateTime, Integer, Text, Boolean, func, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ConfigBackup(Base):
    """设备配置备份记录"""
    __tablename__ = "config_backups"
    __table_args__ = (
        Index("ix_config_backups_device_id", "device_id"),
        Index("ix_config_backups_created_at", "created_at"),
        Index("ix_config_backups_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    backup_type: Mapped[str] = mapped_column(String(16), default="manual")  # manual / scheduled
    config_content: Mapped[str] = mapped_column(Text, nullable=False)
    config_hash: Mapped[str | None] = mapped_column(String(64))
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    line_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="success")  # success / failed
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    device = relationship("Device", back_populates="config_backups")


class BackupSchedule(Base):
    """设备配置备份计划"""
    __tablename__ = "backup_schedules"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=True)
    is_all_devices: Mapped[bool] = mapped_column(Boolean, default=False)  # True = 备份全部设备
    frequency: Mapped[str] = mapped_column(String(16), default="daily")  # daily / weekly / monthly
    day_of_week: Mapped[int | None] = mapped_column(default=None)  # 0=Mon..6=Sun (weekly)
    day_of_month: Mapped[int | None] = mapped_column(default=None)  # 1-28 (monthly)
    hour: Mapped[int] = mapped_column(default=2)  # 0-23
    minute: Mapped[int] = mapped_column(default=0)  # 0-59
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_backup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_backup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    device = relationship("Device", back_populates="backup_schedules")
