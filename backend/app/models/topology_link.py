"""自定义拓扑连线模型 - 用户在已管理设备之间手动建立的链路"""
from datetime import datetime

from sqlalchemy import BigInteger, String, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TopologyLink(Base):
    __tablename__ = "topology_links"
    # 一对设备只允许一条自定义连线（创建时规范化为 source_id < target_id）
    __table_args__ = (
        UniqueConstraint("source_device_id", "target_device_id", name="uq_topology_link_pair"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_device_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("devices.id"), nullable=False)
    target_device_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("devices.id"), nullable=False)
    link_type: Mapped[str] = mapped_column(String(32), default="custom")
    label: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    source_device = relationship("Device", foreign_keys=[source_device_id], lazy="selectin")
    target_device = relationship("Device", foreign_keys=[target_device_id], lazy="selectin")
