"""P2 模型：动态基线、预测结果、健康评分、生命周期"""
from datetime import datetime
from sqlalchemy import Column, BigInteger, String, SmallInteger, Integer, Float, Date, Text, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.database import Base


class MetricBaseline(Base):
    """动态基线表：存储每设备每指标每小时的统计基线"""
    __tablename__ = "metric_baselines"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    device_id = Column(BigInteger, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    metric_name = Column(String(64), nullable=False)
    hour_of_day = Column(SmallInteger, nullable=False)
    p5 = Column(Float)
    p25 = Column(Float)
    p50 = Column(Float)
    p75 = Column(Float)
    p95 = Column(Float)
    stddev = Column(Float)
    sample_count = Column(Integer, default=0)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("device_id", "metric_name", "hour_of_day", name="uq_baseline"),
    )


class PredictionResult(Base):
    """AI预测结果表"""
    __tablename__ = "prediction_results"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    device_id = Column(BigInteger, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    prediction_type = Column(String(32), nullable=False)  # disk_exhaustion / interface_error / cpu_trend
    metric_name = Column(String(64))
    current_value = Column(Float)
    predicted_value = Column(Float)
    predicted_date = Column(Date)
    confidence = Column(Float)  # 0-1
    details = Column(JSONB)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("device_id", "prediction_type", "predicted_date", name="uq_prediction"),
    )


class DeviceHealthScore(Base):
    """设备综合健康评分表"""
    __tablename__ = "device_health_scores"

    device_id = Column(BigInteger, ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True)
    total_score = Column(SmallInteger)  # 0-100
    performance = Column(SmallInteger)  # 性能子分
    stability = Column(SmallInteger)  # 稳定性子分
    hardware = Column(SmallInteger)  # 硬件子分
    lifecycle = Column(SmallInteger)  # 生命周期子分
    details = Column(JSONB)  # 扣分明细
    calculated_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    device = relationship("Device", back_populates="health_score")


class LifecycleDB(Base):
    """厂商EOS/EOL数据库"""
    __tablename__ = "lifecycle_db"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    vendor = Column(String(64), nullable=False)
    model = Column(String(128), nullable=False)
    eos_date = Column(Date)  # 停止销售日期
    eol_date = Column(Date)  # 停止支持日期
    eos_announce = Column(Text)  # 官方公告链接
    source = Column(String(32), default="manual")  # manual/official_api
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("vendor", "model", name="uq_lifecycle_vendor_model"),
    )
