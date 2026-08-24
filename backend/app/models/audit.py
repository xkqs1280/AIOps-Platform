from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AuditLog(Base):
    """操作审计日志：记录敏感操作（登录、设备变更、用户管理、授权等）。"""
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user: Mapped[str] = mapped_column(String(64), default="-", index=True)
    role: Mapped[str] = mapped_column(String(16), default="-")
    module: Mapped[str] = mapped_column(String(32), index=True)     # 如 device / auth / user / license / backup / mail
    action: Mapped[str] = mapped_column(String(32))                 # 如 create / update / delete / login / logout / activate
    detail: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
