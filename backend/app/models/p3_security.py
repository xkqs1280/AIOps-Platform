"""P3 模型：安全事件、威胁情报、合规检查、月度报告"""
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base


class SecurityEvent(Base):
    """安全事件表：存储防火墙/IDS/IPS等安全设备日志"""
    __tablename__ = "security_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    device_id = Column(BigInteger, ForeignKey("devices.id"), nullable=True)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    event_category = Column(String(32))  # intrusion/malware/ddos/policy/anomaly/audit
    event_subcategory = Column(String(64))
    severity = Column(String(16))  # critical/high/medium/low/info
    action = Column(String(16))  # blocked/allowed/detected/dropped
    description = Column(Text)
    src_ip = Column(String(45))
    src_port = Column(Integer)
    dst_ip = Column(String(45))
    dst_port = Column(Integer)
    protocol = Column(String(8))
    app = Column(String(32))
    threat_type = Column(String(64))
    signature_id = Column(String(32))
    cve = Column(String(32))
    threat_score = Column(Float)
    ip_reputation = Column(String(16))  # malicious/suspicious/scanner/normal/unknown
    raw_log = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_security_events_timestamp", "timestamp", postgresql_ops={"timestamp": "DESC"}),
        Index("ix_security_events_device_timestamp", "device_id", "timestamp", postgresql_ops={"timestamp": "DESC"}),
        Index("ix_security_events_src_ip", "src_ip"),
        Index("ix_security_events_dst_ip", "dst_ip"),
        Index("ix_security_events_category_timestamp", "event_category", "timestamp", postgresql_ops={"timestamp": "DESC"}),
    )


class ThreatIntel(Base):
    """威胁情报表：IOC指标库"""
    __tablename__ = "threat_intel"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    indicator = Column(String(256), nullable=False)
    indicator_type = Column(String(16))  # ipv4/domain/url/hash
    threat_type = Column(String(64))  # malware/phishing/c2/scanner
    confidence = Column(SmallInteger)  # 0-100
    source = Column(String(64))  # MISP/微步/奇安信
    first_seen = Column(DateTime(timezone=True))
    last_seen = Column(DateTime(timezone=True))
    tags = Column(ARRAY(String))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("indicator", "indicator_type", name="uq_threat_intel_indicator"),
    )


class ComplianceCheck(Base):
    """合规检查表：设备合规基线检查结果"""
    __tablename__ = "compliance_checks"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    device_id = Column(BigInteger, ForeignKey("devices.id"), nullable=False)
    control_id = Column(String(16))
    control_desc = Column(Text)
    status = Column(String(16))  # compliant/partial/non_compliant/not_applicable
    evidence = Column(Text)
    checked_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("device_id", "control_id", name="uq_compliance_device_control"),
    )


class MonthlyReport(Base):
    """月度报告表：每月运维报告存档"""
    __tablename__ = "monthly_reports"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    report_month = Column(Date, nullable=False)
    tenant_id = Column(String(64), default="default")
    file_path = Column(String(512))
    file_size = Column(BigInteger)
    health_score = Column(SmallInteger)
    alert_summary = Column(JSONB)
    security_summary = Column(JSONB)
    compliance_score = Column(SmallInteger)
    generated_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("report_month", "tenant_id", name="uq_monthly_report"),
    )
