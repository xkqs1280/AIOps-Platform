from datetime import datetime

from sqlalchemy import String, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    ip: Mapped[str] = mapped_column(String(45), nullable=False)
    vendor: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    serial_number: Mapped[str | None] = mapped_column(String(64))
    snmp_version: Mapped[str | None] = mapped_column(String(8), default="v2c")
    snmp_community: Mapped[str | None] = mapped_column(String(128))
    mgmt_protocol: Mapped[str | None] = mapped_column(String(8), default="ssh")
    mgmt_port: Mapped[int | None] = mapped_column(default=22)
    mgmt_username: Mapped[str | None] = mapped_column(String(64))
    mgmt_password: Mapped[str | None] = mapped_column(String(128))
    device_type: Mapped[str | None] = mapped_column(String(32))
    group_name: Mapped[str | None] = mapped_column(String(64))
    location: Mapped[str | None] = mapped_column(String(256))
    warranty_expire: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    eos_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    eol_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="unknown")
    cpu_usage: Mapped[float | None] = mapped_column(default=None)
    memory_usage: Mapped[float | None] = mapped_column(default=None)
    temperature: Mapped[float | None] = mapped_column(default=None)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # lazy="noload"：避免设备列表/拓扑/指标采集等 select(Device) 全表查询
    # 自动附带加载关联数据（N+1），按需显式 joinedload/selectinload。
    alerts = relationship("Alert", back_populates="device", lazy="noload")
    health_score = relationship("DeviceHealthScore", back_populates="device", uselist=False, lazy="noload")
    config_backups = relationship("ConfigBackup", back_populates="device", lazy="noload")
    backup_schedules = relationship("BackupSchedule", back_populates="device", lazy="noload")
