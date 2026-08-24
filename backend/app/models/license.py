"""平台授权信息表 — 存储当前生效的激活记录（测试版 / 全功能版）"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LicenseInfo(Base):
    __tablename__ = "license_info"

    id: Mapped[int] = mapped_column(primary_key=True)
    # trial=测试版(3个月) / full=全功能版(永久)
    version: Mapped[str] = mapped_column(String(16), default="trial")
    # 激活时保存的原始激活码（验签+指纹比对通过后写入）
    license_code: Mapped[str] = mapped_column(String(1024))
    # 激活时的机器指纹（应与本机当前指纹一致，供审计）
    fingerprint: Mapped[str] = mapped_column(String(64))
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # full 版为 NULL（永久授权）
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
