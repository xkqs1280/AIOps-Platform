from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DeviceComponent(Base):
    """设备硬件组件明细（实体 MIB 采集，板卡/电源/风扇/传感器等）。

    由设备「同步」时从 ENTITY-MIB 全表采集写入，随同步刷新。
    """

    __tablename__ = "device_components"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True, nullable=False
    )
    phys_index: Mapped[int] = mapped_column(Integer, nullable=False, comment="entPhysicalIndex")
    name: Mapped[str | None] = mapped_column(String(128))
    descr: Mapped[str | None] = mapped_column(String(512))
    model_name: Mapped[str | None] = mapped_column(String(128))
    serial_number: Mapped[str | None] = mapped_column(String(64))
    hardware_rev: Mapped[str | None] = mapped_column(String(64))
    firmware_rev: Mapped[str | None] = mapped_column(String(64))
    software_rev: Mapped[str | None] = mapped_column(String(128))
    mfg_name: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
