from app.models.device import Device
from app.models.device_component import DeviceComponent
from app.models.alert import Alert, AlertRule
from app.models.p2_baseline import MetricBaseline, PredictionResult, DeviceHealthScore, LifecycleDB
from app.models.p3_security import SecurityEvent, ThreatIntel, ComplianceCheck
from app.models.config_backup import ConfigBackup, BackupSchedule
from app.models.metric_record import MetricRecord
from app.models.inspection import InspectionTask, InspectionDeviceResult
from app.models.topology_link import TopologyLink
from app.models.external_threat import ExternalThreatSnapshot
from app.models.user import User
from app.models.business_monitor import BusinessGroup, BusinessTerminal, BusinessAlert
from app.models.license import LicenseInfo
from app.models.device_dependency import DeviceDependency
from app.models.notify_channel import NotifyChannel
from app.models.device_log import DeviceLog, DeviceLogHostConfig
# 以下三个模块此前只在各自 router 里被间接导入。生产上没问题（main.py 先导入
# routers 再 init_db），但只要有人单独 `import app.models`（测试、脚本、离线工具），
# Base.metadata 就缺这三张表，create_all 会静默漏建。这里统一登记。
from app.models.ai import AiSetting, AiCache, AiKbDoc, AiKbChunk, AiLog
from app.models.audit import AuditLog
from app.models.mail_setting import MailSetting

__all__ = [
    "Device", "DeviceComponent", "Alert", "AlertRule",
    "MetricBaseline", "PredictionResult", "DeviceHealthScore", "LifecycleDB",
    "SecurityEvent", "ThreatIntel", "ComplianceCheck",
    "ConfigBackup", "BackupSchedule", "MetricRecord",
    "InspectionTask", "InspectionDeviceResult",
    "TopologyLink", "ExternalThreatSnapshot", "User",
    "BusinessGroup", "BusinessTerminal", "BusinessAlert",
    "LicenseInfo", "DeviceDependency", "NotifyChannel",
    "DeviceLog", "DeviceLogHostConfig",
    "AiSetting", "AiCache", "AiKbDoc", "AiKbChunk", "AiLog",
    "AuditLog", "MailSetting",
]
