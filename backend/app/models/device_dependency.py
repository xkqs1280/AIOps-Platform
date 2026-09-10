"""设备依赖关系模型 —— 用于告警拓扑依赖抑制。

语义：`device_id` 依赖 `depends_on_device_id`（前者是下游/接入侧，后者是上游/汇聚侧）。
当上游设备离线或存在「设备不可达」告警时，下游设备因失去上联而产生的连带告警
应被抑制，避免一台核心设备掉线导致全网设备一起刷屏。

设计要点：
  - 独立于 topology_links：视觉拓扑连线的方向在创建时被规范化为 source<target 而丢失，
    无法表达上下游；依赖关系需要明确方向，故单独建表；
  - 一台设备可配置多条依赖（双上联场景），unique(device, depends_on) 去重；
  - 不配置任何依赖时，抑制逻辑完全不生效，对存量部署零影响。
"""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class DeviceDependency(Base):
    __tablename__ = "device_dependencies"
    __table_args__ = (
        UniqueConstraint("device_id", "depends_on_device_id", name="uq_device_dependency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 下游设备（产生连带告警的一方）
    device_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("devices.id"), nullable=False, index=True)
    # 上游设备（掉线时导致下游告警被抑制的一方）
    depends_on_device_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("devices.id"), nullable=False, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    device = relationship("Device", foreign_keys=[device_id], lazy="selectin")
    depends_on = relationship("Device", foreign_keys=[depends_on_device_id], lazy="selectin")
