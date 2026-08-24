from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MailSetting(Base):
    """邮件告警配置（全局单行）。"""
    __tablename__ = "mail_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    smtp_host: Mapped[str] = mapped_column(String(128), default="")
    smtp_port: Mapped[int] = mapped_column(Integer, default=465)
    smtp_user: Mapped[str] = mapped_column(String(128), default="")
    smtp_password: Mapped[str] = mapped_column(String(255), default="")
    use_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    sender: Mapped[str] = mapped_column(String(128), default="")
    recipients: Mapped[str] = mapped_column(String(512), default="")  # 逗号分隔
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
