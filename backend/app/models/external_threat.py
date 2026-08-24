"""外部威胁情报快照模型 - 抓取 FireHOL 等开放威胁情报源后的聚合快照"""
from datetime import datetime

from sqlalchemy import BigInteger, String, DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ExternalThreatSnapshot(Base):
    __tablename__ = "external_threat_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    total_entries: Mapped[int] = mapped_column(BigInteger, default=0)
    type_data: Mapped[str] = mapped_column(Text, default="{}")     # {"高危恶意源": 123, ...}
    country_data: Mapped[str] = mapped_column(Text, default="{}")  # {"US": 12, "CN": 3, ...}
    china_entries: Mapped[int] = mapped_column(default=0)          # 抽样中来源国为中国的 IP 数
    sampled_ips: Mapped[int] = mapped_column(default=0)            # 实际完成地理标注的抽样 IP 数
    source: Mapped[str] = mapped_column(String(128), default="FireHOL + ip-api.com")
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
