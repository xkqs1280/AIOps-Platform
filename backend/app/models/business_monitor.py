"""重要业务监控模型：业务分组、监控终端、离线告警记录"""
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class BusinessGroup(Base):
    """业务分组（如：厂区监控、门禁、办公终端）"""
    __tablename__ = "business_groups"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    terminals = relationship("BusinessTerminal", back_populates="group",
                             cascade="all, delete-orphan")


class BusinessTerminal(Base):
    """业务监控终端（摄像头/门禁/其他终端）"""
    __tablename__ = "business_terminals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("business_groups.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    ip: Mapped[str] = mapped_column(String(45), nullable=False)
    mac: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(String(256))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # 探测状态
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # unknown/online/offline
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_online_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_offline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    offline_count: Mapped[int] = mapped_column(Integer, default=0)  # 连续离线轮询次数
    online_count: Mapped[int] = mapped_column(Integer, default=0)   # 连续在线轮询次数

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    group = relationship("BusinessGroup", back_populates="terminals")
    alerts = relationship("BusinessAlert", back_populates="terminal",
                          cascade="all, delete-orphan")


class BusinessAlert(Base):
    """业务监控告警记录（终端离线 / 恢复）"""
    __tablename__ = "business_alerts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    terminal_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("business_terminals.id", ondelete="CASCADE"), nullable=False
    )
    terminal_name: Mapped[str] = mapped_column(String(128))  # 快照
    terminal_ip: Mapped[str] = mapped_column(String(45))
    alert_type: Mapped[str] = mapped_column(String(16), nullable=False)  # offline / recovered
    severity: Mapped[str] = mapped_column(String(16), default="critical")  # critical/warning/info
    message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    terminal = relationship("BusinessTerminal", back_populates="alerts")
