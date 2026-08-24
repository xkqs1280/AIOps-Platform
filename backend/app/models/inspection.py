from datetime import datetime

from sqlalchemy import String, DateTime, Integer, Text, func, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class InspectionTask(Base):
    """H3C 设备巡检任务"""
    __tablename__ = "inspection_tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending / running / completed / failed
    device_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False)
    total_devices: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    excel_path: Mapped[str | None] = mapped_column(String(512))
    word_path: Mapped[str | None] = mapped_column(String(512))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # lazy="noload"：列表接口不自动加载（device_results 含 raw_output/parsed_data 大字段），
    # 详情接口显式 joinedload。
    device_results = relationship("InspectionDeviceResult", back_populates="task", lazy="noload", cascade="all, delete-orphan")


class InspectionDeviceResult(Base):
    """巡检任务中每台设备的结果"""
    __tablename__ = "inspection_device_results"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("inspection_tasks.id", ondelete="CASCADE"), nullable=False)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    device_name: Mapped[str] = mapped_column(String(128))
    device_ip: Mapped[str] = mapped_column(String(45))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending / running / success / failed
    raw_output: Mapped[str | None] = mapped_column(Text)
    parsed_data: Mapped[dict | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    task = relationship("InspectionTask", back_populates="device_results")
    device = relationship("Device")
