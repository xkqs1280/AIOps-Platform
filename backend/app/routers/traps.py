"""SNMP Trap 接收端点 - 接收 snmptrapd 转发的 trap 数据，自动创建告警

基于华为 S5700 MIB 树 (mibtree.xml) 提取的完整 Trap OID 映射：
  - 标准 SNMP Trap (RFC 2863/3418)
  - 华为实体扩展 Trap (hwEntityExtTraps)
  - 华为系统管理 Trap (huaweiSystemManMIB)
  - 华为堆叠 Trap (hwStackEventsV2)
  - 华为接口流量 Trap (hwIfFlowDown/Up)
  - 路由协议 Trap (OSPF/BGP/VRRP)
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field

from app.database import get_db
from app.models.device import Device
from app.models.alert import Alert
from app.services.rate_limit import limit_ingest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/traps", tags=["SNMP Trap"])

tz_8 = timezone(timedelta(hours=8))

# ============================================================
# Trap OID 定义
# ============================================================

# --- 标准 SNMP Trap (1.3.6.1.6.3.1.1.5.x) ---
COLD_START_OID = "1.3.6.1.6.3.1.1.5.1"
WARM_START_OID = "1.3.6.1.6.3.1.1.5.2"
LINK_DOWN_OID = "1.3.6.1.6.3.1.1.5.3"
LINK_UP_OID = "1.3.6.1.6.3.1.1.5.4"
AUTH_FAILURE_OID = "1.3.6.1.6.3.1.1.5.5"

# --- 华为实体扩展 Trap (1.3.6.1.4.1.2011.5.25.31.2.0.x) ---
HW_TEMP_THRESHOLD_OID = "1.3.6.1.4.1.2011.5.25.31.2.0.1"
HW_VOLTAGE_LOW_OID = "1.3.6.1.4.1.2011.5.25.31.2.0.2"
HW_VOLTAGE_HIGH_OID = "1.3.6.1.4.1.2011.5.25.31.2.0.3"
HW_CPU_THRESHOLD_OID = "1.3.6.1.4.1.2011.5.25.31.2.0.4"
HW_MEM_THRESHOLD_OID = "1.3.6.1.4.1.2011.5.25.31.2.0.5"
HW_OPER_ENABLED_OID = "1.3.6.1.4.1.2011.5.25.31.2.0.6"
HW_OPER_DISABLED_OID = "1.3.6.1.4.1.2011.5.25.31.2.0.7"
HW_BOARD_ABNORMAL_OID = "1.3.6.1.4.1.2011.5.25.31.2.0.8"
HW_BOARD_NORMAL_OID = "1.3.6.1.4.1.2011.5.25.31.2.0.9"
HW_PORT_ABNORMAL_OID = "1.3.6.1.4.1.2011.5.25.31.2.0.10"
HW_PORT_NORMAL_OID = "1.3.6.1.4.1.2011.5.25.31.2.0.11"
HW_CPU_NORMAL_OID = "1.3.6.1.4.1.2011.5.25.31.2.0.12"

# --- 华为系统管理 Trap (1.3.6.1.4.1.2011.5.25.19.2.x) ---
HW_CLOCK_CHANGED_OID = "1.3.6.1.4.1.2011.5.25.19.2.1"
HW_RELOAD_OID = "1.3.6.1.4.1.2011.5.25.19.2.2"
HW_SLAVE_SWITCH_OK_OID = "1.3.6.1.4.1.2011.5.25.19.2.8"
HW_SLAVE_SWITCH_FAIL_OID = "1.3.6.1.4.1.2011.5.25.19.2.9"
HW_CFG_FILE_ERROR_OID = "1.3.6.1.4.1.2011.5.25.19.2.14"
HW_IMAGE_ERROR_OID = "1.3.6.1.4.1.2011.5.25.19.2.15"

# --- 华为堆叠 Trap (1.3.6.1.4.1.2011.5.25.183.1.22.x) ---
HW_STACK_LINK_UP_OID = "1.3.6.1.4.1.2011.5.25.183.1.22.1"
HW_STACK_LINK_DOWN_OID = "1.3.6.1.4.1.2011.5.25.183.1.22.2"
HW_STACK_STANDBY_CHANGE_OID = "1.3.6.1.4.1.2011.5.25.183.1.22.3"
HW_STACK_SWITCHOVER_OID = "1.3.6.1.4.1.2011.5.25.183.1.22.4"
HW_STACK_RESTART_OID = "1.3.6.1.4.1.2011.5.25.183.1.22.5"
HW_STACK_MEMBER_ADD_OID = "1.3.6.1.4.1.2011.5.25.183.1.22.6"
HW_STACK_MEMBER_LEAVE_OID = "1.3.6.1.4.1.2011.5.25.183.1.22.7"

# --- 华为接口流量 Trap (1.3.6.1.4.1.2011.5.25.41.3.x) ---
HW_IF_FLOW_DOWN_OID = "1.3.6.1.4.1.2011.5.25.41.3.5"
HW_IF_FLOW_UP_OID = "1.3.6.1.4.1.2011.5.25.41.3.6"

# --- 华为硬盘/存储 Trap (1.3.6.1.4.1.2011.5.2.2.3.0.x) ---
HW_HARDDISK_OVERFLOW_OID = "1.3.6.1.4.1.2011.5.2.2.3.0.1"
HW_HARDDISK_THRESHOLD_OID = "1.3.6.1.4.1.2011.5.2.2.3.0.2"
HW_HARDDISK_OK_OID = "1.3.6.1.4.1.2011.5.2.2.3.0.3"

# --- 华为通用告警 Trap (1.3.6.1.4.1.2011.1.3.4.0.x) ---
HW_RISING_ALARM_OID = "1.3.6.1.4.1.2011.1.3.4.0.1"
HW_FALLING_ALARM_OID = "1.3.6.1.4.1.2011.1.3.4.0.2"

# --- 路由协议 Trap ---
# BGP (1.3.6.1.2.1.15.7.0.x)
BGP_ESTABLISHED_OID = "1.3.6.1.2.1.15.7.0.1"
BGP_BACKWARD_OID = "1.3.6.1.2.1.15.7.0.2"
# OSPF (1.3.6.1.2.1.14.16.2.x)
OSPF_IF_STATE_CHANGE_OID = "1.3.6.1.2.1.14.16.2.1"
OSPF_NBR_STATE_CHANGE_OID = "1.3.6.1.2.1.14.16.2.2"
# VRRP (1.3.6.1.2.1.68.0.x)
VRRP_NEW_MASTER_OID = "1.3.6.1.2.1.68.0.1"
VRRP_AUTH_FAILURE_OID = "1.3.6.1.2.1.68.0.2"
# Entity MIB (1.3.6.1.2.1.47.2.0.x)
ENTITY_CONFIG_CHANGE_OID = "1.3.6.1.2.1.47.2.0.1"

# --- LLDP Trap ---
LLDP_TABLES_CHANGE_OID = "1.0.8802.1.1.2.0.0.1"

# ============================================================
# MIB 名称 -> 数字 OID 映射 (支持 snmptrapd MIB 模式输出)
# ============================================================
TRAP_OID_MAP = {
    # 标准
    "IF-MIB::linkDown": LINK_DOWN_OID,
    "IF-MIB::linkUp": LINK_UP_OID,
    "SNMPv2-MIB::coldStart": COLD_START_OID,
    "SNMPv2-MIB::warmStart": WARM_START_OID,
    "SNMPv2-MIB::authenticationFailure": AUTH_FAILURE_OID,
    # 华为实体
    "HW-ENTITY-EXT-MIB::hwEntityExtTemperatureThresholdNotification": HW_TEMP_THRESHOLD_OID,
    "HW-ENTITY-EXT-MIB::hwEntityExtVoltageLowThresholdNotification": HW_VOLTAGE_LOW_OID,
    "HW-ENTITY-EXT-MIB::hwEntityExtVoltageHighThresholdNotification": HW_VOLTAGE_HIGH_OID,
    "HW-ENTITY-EXT-MIB::hwEntityExtCpuUsageThresholdNotfication": HW_CPU_THRESHOLD_OID,
    "HW-ENTITY-EXT-MIB::hwEntityExtMemUsageThresholdNotification": HW_MEM_THRESHOLD_OID,
    "HW-ENTITY-EXT-MIB::hwEntityExtOperEnabled": HW_OPER_ENABLED_OID,
    "HW-ENTITY-EXT-MIB::hwEntityExtOperDisabled": HW_OPER_DISABLED_OID,
    "HW-ENTITY-EXT-MIB::hwEntityExtMonitorBoardAbnormalNotification": HW_BOARD_ABNORMAL_OID,
    "HW-ENTITY-EXT-MIB::hwEntityExtMonitorBoardNormalNotification": HW_BOARD_NORMAL_OID,
    "HW-ENTITY-EXT-MIB::hwEntityExtMonitorPortAbnormalNotification": HW_PORT_ABNORMAL_OID,
    "HW-ENTITY-EXT-MIB::hwEntityExtMonitorPortNormalNotification": HW_PORT_NORMAL_OID,
    "HW-ENTITY-EXT-MIB::hwEntityExtCpuUsageThresholdNormalNotfication": HW_CPU_NORMAL_OID,
    # 华为系统
    "HUAWEI-SYSTEM-MAN-MIB::hwSysReloadNotification": HW_RELOAD_OID,
    "HUAWEI-SYSTEM-MAN-MIB::hwSysClockChangedNotification": HW_CLOCK_CHANGED_OID,
    "HUAWEI-SYSTEM-MAN-MIB::hwSysSlaveSwitchSuccessNotification": HW_SLAVE_SWITCH_OK_OID,
    "HUAWEI-SYSTEM-MAN-MIB::hwSysSlaveSwitchFailNotification": HW_SLAVE_SWITCH_FAIL_OID,
    "HUAWEI-SYSTEM-MAN-MIB::hwSysCfgFileErrorNotification": HW_CFG_FILE_ERROR_OID,
    "HUAWEI-SYSTEM-MAN-MIB::hwSysImageErrorNotification": HW_IMAGE_ERROR_OID,
    # 华为堆叠
    "HW-STACK-MIB::hwStackLinkDown": HW_STACK_LINK_DOWN_OID,
    "HW-STACK-MIB::hwStackLinkUp": HW_STACK_LINK_UP_OID,
    "HW-STACK-MIB::hwStackSystemRestart": HW_STACK_RESTART_OID,
    "HW-STACK-MIB::hwStackStackMemberLeave": HW_STACK_MEMBER_LEAVE_OID,
    # 华为接口流量
    "HW-IF-MIB::hwIfFlowDown": HW_IF_FLOW_DOWN_OID,
    "HW-IF-MIB::hwIfFlowUp": HW_IF_FLOW_UP_OID,
    # BGP/OSPF/VRRP
    "BGP4-MIB::bgpEstablished": BGP_ESTABLISHED_OID,
    "BGP4-MIB::bgpBackwardTransition": BGP_BACKWARD_OID,
    "OSPF-MIB::ospfIfStateChange": OSPF_IF_STATE_CHANGE_OID,
    "OSPF-MIB::ospfNbrStateChange": OSPF_NBR_STATE_CHANGE_OID,
    "VRRP-MIB::vrrpTrapNewMaster": VRRP_NEW_MASTER_OID,
    "VRRP-MIB::vrrpTrapAuthFailure": VRRP_AUTH_FAILURE_OID,
    # Entity
    "ENTITY-MIB::entConfigChange": ENTITY_CONFIG_CHANGE_OID,
    # LLDP
    "LLDP-MIB::lldpRemTablesChange": LLDP_TABLES_CHANGE_OID,
}


class TrapData(BaseModel):
    source_ip: str = Field(..., max_length=45)
    trap_oid: str | None = Field(None, max_length=256)
    uptime: str | None = Field(None, max_length=128)
    varbinds: dict[str, str] = Field(default_factory=dict, max_length=100)


def normalize_trap_oid(oid: str) -> str:
    """将 MIB 名称格式转换为数字 OID，或原样返回"""
    oid = oid.strip()
    # 直接匹配 MIB 名称
    if oid in TRAP_OID_MAP:
        return TRAP_OID_MAP[oid]
    # 去掉前缀的点号
    oid = oid.lstrip(".")
    return oid


def extract_interface_info(varbinds: dict) -> str:
    """从 trap varbinds 中提取接口名称（支持 MIB 名称和数字 OID 格式）"""
    # ifDescr OID = 1.3.6.1.2.1.2.2.1.2
    for key, value in varbinds.items():
        if "ifDescr" in key or key.startswith("1.3.6.1.2.1.2.2.1.2"):
            return value.strip().strip('"').strip("'")
    # ifName OID = 1.3.6.1.2.1.31.1.1.1.1
    for key, value in varbinds.items():
        if "ifName" in key or key.startswith("1.3.6.1.2.1.31.1.1.1.1"):
            return value.strip().strip('"').strip("'")
    # Fallback: ifIndex = 1.3.6.1.2.1.2.2.1.1
    for key, value in varbinds.items():
        if "ifIndex" in key or key.startswith("1.3.6.1.2.1.2.2.1.1"):
            return f"ifIndex={value}"
    return "unknown"


def extract_entity_info(varbinds: dict) -> str:
    """从 trap varbinds 中提取实体（单板/端口）信息"""
    # entPhysicalName OID = 1.3.6.1.2.1.47.1.1.1.1.7
    for key, value in varbinds.items():
        if "entPhysicalName" in key or key.startswith("1.3.6.1.2.1.47.1.1.1.1.7"):
            return value.strip().strip('"').strip("'")
    # hwEntPhysicalName (华为扩展)
    for key, value in varbinds.items():
        if "hwEntPhysicalName" in key:
            return value.strip().strip('"').strip("'")
    # entPhysicalDescr
    for key, value in varbinds.items():
        if "entPhysicalDescr" in key or key.startswith("1.3.6.1.2.1.47.1.1.1.1.2"):
            return value.strip().strip('"').strip("'")
    return "未知实体"


def extract_threshold_info(varbinds: dict) -> str:
    """提取阈值信息（温度/电压/CPU/内存等）"""
    info_parts = []
    for key, value in varbinds.items():
        k_lower = key.lower()
        if any(kw in k_lower for kw in ["temperature", "voltage", "cpu", "mem", "threshold", "hwentity"]):
            clean_val = value.strip().strip('"').strip("'")
            # 提取短名称
            short_key = key.split("::")[-1].split(".")[-1] if "::" in key else key.split(".")[-1]
            info_parts.append(f"{short_key}={clean_val}")
    return ", ".join(info_parts) if info_parts else ""


def _get_varbind_value(varbinds: dict, *oid_patterns: str) -> Optional[str]:
    """从 varbinds 中按 OID 模式匹配值"""
    for key, value in varbinds.items():
        key_normalized = key.replace(".", "").replace("::", "").lower()
        for pattern in oid_patterns:
            if pattern.lower() in key_normalized:
                return value.strip().strip('"').strip("'")
    return None


async def _resolve_related_alerts(
    db: AsyncSession, device_id: int, rule_name_pattern: str, interface: str = None
) -> int:
    """自动恢复相关的 active 告警，返回恢复数量"""
    conditions = [
        Alert.device_id == device_id,
        Alert.status == "active",
        Alert.rule_name.like(rule_name_pattern),
    ]
    if interface:
        conditions.append(Alert.message.like(f"%{interface}%"))

    result = await db.execute(select(Alert).where(and_(*conditions)))
    alerts = result.scalars().all()
    now = datetime.now(timezone.utc)
    for a in alerts:
        a.status = "resolved"
        a.resolved_at = now
    return len(alerts)


@router.post("")
async def receive_trap(
    trap: TrapData,
    _: None = Depends(limit_ingest),
    db: AsyncSession = Depends(get_db),
):
    """接收 SNMP Trap 并自动创建告警

    支持的 Trap 类型：
    - 标准: linkDown/linkUp/coldStart/warmStart/authenticationFailure
    - 华为实体: 温度/电压/CPU/内存/单板/端口 异常与恢复
    - 华为系统: 重启/主备切换/配置错误/镜像错误
    - 华为堆叠: 堆叠链路/成员变化
    - 华为接口: 流量中断/恢复
    - 路由协议: BGP/OSPF/VRRP 状态变化
    - 实体配置变更/LLDP 变化
    """
    # 按 IP 查找设备
    result = await db.execute(select(Device).where(Device.ip == trap.source_ip))
    device = result.scalar_one_or_none()

    if not device:
        logger.info(f"Trap from unknown device {trap.source_ip}, ignored")
        return {"status": "ignored", "reason": "device not found", "ip": trap.source_ip}

    trap_oid = normalize_trap_oid(trap.trap_oid or "")
    now = datetime.now(timezone.utc)

    logger.info(
        f"Trap received: device={device.name}({device.ip}) "
        f"oid={trap_oid} varbinds={list(trap.varbinds.keys())}"
    )

    # ============================================================
    # 1. 标准 Trap 处理
    # ============================================================

    if trap_oid == LINK_DOWN_OID:
        iface = extract_interface_info(trap.varbinds)
        existing = await db.execute(
            select(Alert).where(
                and_(
                    Alert.device_id == device.id,
                    Alert.status == "active",
                    Alert.rule_name == "SNMP Trap: linkDown",
                    Alert.message.like(f"%{iface}%"),
                )
            )
        )
        if existing.scalar_one_or_none():
            logger.info(f"Duplicate linkDown for {iface} on {device.name}, skipped")
            return {"status": "duplicate", "message": f"linkDown for {iface} already active"}

        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: linkDown",
            severity="critical",
            message=f"接口 {iface} 状态变为 DOWN（设备主动上报 Trap）",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        if device.status == "online":
            device.status = "warning"
        logger.warning(f"Alert created: linkDown on {device.name} interface {iface}")

    elif trap_oid == LINK_UP_OID:
        iface = extract_interface_info(trap.varbinds)
        resolved = await _resolve_related_alerts(
            db, device.id, "SNMP Trap: linkDown", iface
        )
        if resolved:
            logger.info(f"Resolved {resolved} linkDown alert(s) for {iface} on {device.name}")

        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: linkUp",
            severity="info",
            message=f"接口 {iface} 状态恢复 UP（设备主动上报 Trap）",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: linkUp on {device.name} interface {iface}")

    elif trap_oid == COLD_START_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: coldStart",
            severity="major",
            message="设备冷启动（coldStart），可能发生断电或崩溃重启",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.warning(f"Alert created: coldStart on {device.name}")

    elif trap_oid == WARM_START_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: warmStart",
            severity="minor",
            message="设备热启动（warmStart），配置可能已重新加载",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: warmStart on {device.name}")

    elif trap_oid == AUTH_FAILURE_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: authenticationFailure",
            severity="warning",
            message="SNMP 认证失败，可能存在非法访问尝试",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.warning(f"Alert created: authenticationFailure on {device.name}")

    # ============================================================
    # 2. 华为实体扩展 Trap
    # ============================================================

    elif trap_oid == HW_TEMP_THRESHOLD_OID:
        entity = extract_entity_info(trap.varbinds)
        extra = extract_threshold_info(trap.varbinds)
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 温度超限",
            severity="critical",
            message=f"实体 {entity} 温度超过阈值{' (' + extra + ')' if extra else ''}",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        if device.status == "online":
            device.status = "warning"
        logger.warning(f"Alert created: temperature threshold on {device.name} {entity}")

    elif trap_oid == HW_VOLTAGE_LOW_OID:
        entity = extract_entity_info(trap.varbinds)
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 电压过低",
            severity="critical",
            message=f"实体 {entity} 电压低于阈值",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        if device.status == "online":
            device.status = "warning"
        logger.warning(f"Alert created: voltage low on {device.name} {entity}")

    elif trap_oid == HW_VOLTAGE_HIGH_OID:
        entity = extract_entity_info(trap.varbinds)
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 电压过高",
            severity="critical",
            message=f"实体 {entity} 电压高于阈值",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        if device.status == "online":
            device.status = "warning"
        logger.warning(f"Alert created: voltage high on {device.name} {entity}")

    elif trap_oid == HW_CPU_THRESHOLD_OID:
        entity = extract_entity_info(trap.varbinds)
        extra = extract_threshold_info(trap.varbinds)
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: CPU使用率超限",
            severity="major",
            message=f"实体 {entity} CPU 使用率超过阈值{' (' + extra + ')' if extra else ''}",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        if device.status == "online":
            device.status = "warning"
        logger.warning(f"Alert created: CPU threshold on {device.name} {entity}")

    elif trap_oid == HW_MEM_THRESHOLD_OID:
        entity = extract_entity_info(trap.varbinds)
        extra = extract_threshold_info(trap.varbinds)
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 内存使用率超限",
            severity="major",
            message=f"实体 {entity} 内存使用率超过阈值{' (' + extra + ')' if extra else ''}",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        if device.status == "online":
            device.status = "warning"
        logger.warning(f"Alert created: memory threshold on {device.name} {entity}")

    elif trap_oid == HW_CPU_NORMAL_OID:
        entity = extract_entity_info(trap.varbinds)
        resolved = await _resolve_related_alerts(db, device.id, "SNMP Trap: CPU使用率超限", entity)
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: CPU使用率恢复正常",
            severity="info",
            message=f"实体 {entity} CPU 使用率恢复正常 (已恢复 {resolved} 条告警)",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: CPU normal on {device.name} {entity}")

    elif trap_oid == HW_OPER_DISABLED_OID:
        entity = extract_entity_info(trap.varbinds)
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 实体禁用",
            severity="critical",
            message=f"实体 {entity} 已被禁用",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        if device.status == "online":
            device.status = "warning"
        logger.warning(f"Alert created: entity disabled on {device.name} {entity}")

    elif trap_oid == HW_OPER_ENABLED_OID:
        entity = extract_entity_info(trap.varbinds)
        resolved = await _resolve_related_alerts(db, device.id, "SNMP Trap: 实体禁用", entity)
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 实体启用",
            severity="info",
            message=f"实体 {entity} 已启用 (已恢复 {resolved} 条告警)",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: entity enabled on {device.name} {entity}")

    elif trap_oid == HW_BOARD_ABNORMAL_OID:
        entity = extract_entity_info(trap.varbinds)
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 单板异常",
            severity="critical",
            message=f"单板 {entity} 运行异常，可能影响业务",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        if device.status != "offline":
            device.status = "warning"
        logger.warning(f"Alert created: board abnormal on {device.name} {entity}")

    elif trap_oid == HW_BOARD_NORMAL_OID:
        entity = extract_entity_info(trap.varbinds)
        resolved = await _resolve_related_alerts(db, device.id, "SNMP Trap: 单板异常", entity)
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 单板恢复正常",
            severity="info",
            message=f"单板 {entity} 已恢复正常 (已恢复 {resolved} 条告警)",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: board normal on {device.name} {entity}")

    elif trap_oid == HW_PORT_ABNORMAL_OID:
        entity = extract_entity_info(trap.varbinds)
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 端口异常",
            severity="critical",
            message=f"端口 {entity} 运行异常",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        if device.status == "online":
            device.status = "warning"
        logger.warning(f"Alert created: port abnormal on {device.name} {entity}")

    elif trap_oid == HW_PORT_NORMAL_OID:
        entity = extract_entity_info(trap.varbinds)
        resolved = await _resolve_related_alerts(db, device.id, "SNMP Trap: 端口异常", entity)
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 端口恢复正常",
            severity="info",
            message=f"端口 {entity} 已恢复正常 (已恢复 {resolved} 条告警)",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: port normal on {device.name} {entity}")

    # ============================================================
    # 3. 华为系统管理 Trap
    # ============================================================

    elif trap_oid == HW_RELOAD_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 设备重启",
            severity="critical",
            message="设备执行重启操作（hwSysReloadNotification），业务将中断",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        device.status = "offline"
        logger.warning(f"Alert created: device reload on {device.name}")

    elif trap_oid == HW_CLOCK_CHANGED_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 系统时钟变更",
            severity="minor",
            message="设备系统时钟被修改，可能影响日志和告警时间戳",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: clock changed on {device.name}")

    elif trap_oid == HW_SLAVE_SWITCH_OK_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 主备切换成功",
            severity="minor",
            message="设备主备倒换成功（hwSysSlaveSwitchSuccessNotification）",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: slave switch success on {device.name}")

    elif trap_oid == HW_SLAVE_SWITCH_FAIL_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 主备切换失败",
            severity="critical",
            message="设备主备倒换失败（hwSysSlaveSwitchFailNotification），可能影响冗余",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        if device.status == "online":
            device.status = "warning"
        logger.warning(f"Alert created: slave switch fail on {device.name}")

    elif trap_oid == HW_CFG_FILE_ERROR_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 配置文件错误",
            severity="critical",
            message="设备配置文件出错（hwSysCfgFileErrorNotification），可能丢失配置",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.warning(f"Alert created: config file error on {device.name}")

    elif trap_oid == HW_IMAGE_ERROR_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 系统镜像错误",
            severity="critical",
            message="设备系统镜像文件出错（hwSysImageErrorNotification），可能无法启动",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.warning(f"Alert created: image error on {device.name}")

    # ============================================================
    # 4. 华为堆叠 Trap
    # ============================================================

    elif trap_oid == HW_STACK_LINK_DOWN_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 堆叠链路断开",
            severity="critical",
            message="堆叠链路断开（hwStackLinkDown），可能影响堆叠稳定性",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        if device.status == "online":
            device.status = "warning"
        logger.warning(f"Alert created: stack link down on {device.name}")

    elif trap_oid == HW_STACK_LINK_UP_OID:
        resolved = await _resolve_related_alerts(db, device.id, "SNMP Trap: 堆叠链路断开")
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 堆叠链路恢复",
            severity="info",
            message=f"堆叠链路恢复 UP (已恢复 {resolved} 条告警)",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: stack link up on {device.name}")

    elif trap_oid == HW_STACK_RESTART_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 堆叠系统重启",
            severity="critical",
            message="堆叠系统重启（hwStackSystemRestart）",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        device.status = "offline"
        logger.warning(f"Alert created: stack system restart on {device.name}")

    elif trap_oid == HW_STACK_MEMBER_LEAVE_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 堆叠成员离开",
            severity="critical",
            message="堆叠成员离开（hwStackStackMemberLeave），堆叠拓扑变更",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        if device.status == "online":
            device.status = "warning"
        logger.warning(f"Alert created: stack member leave on {device.name}")

    elif trap_oid == HW_STACK_SWITCHOVER_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 堆叠主备倒换",
            severity="minor",
            message="堆叠主备倒换（hwStackSwitchOver）",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: stack switchover on {device.name}")

    elif trap_oid == HW_STACK_MEMBER_ADD_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 堆叠成员加入",
            severity="info",
            message="新成员加入堆叠（hwStackStackMemberAdd）",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: stack member add on {device.name}")

    # ============================================================
    # 5. 华为接口流量 Trap
    # ============================================================

    elif trap_oid == HW_IF_FLOW_DOWN_OID:
        iface = extract_interface_info(trap.varbinds)
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 接口流量中断",
            severity="critical",
            message=f"接口 {iface} 流量中断（hwIfFlowDown）",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        if device.status == "online":
            device.status = "warning"
        logger.warning(f"Alert created: ifFlowDown on {device.name} {iface}")

    elif trap_oid == HW_IF_FLOW_UP_OID:
        iface = extract_interface_info(trap.varbinds)
        resolved = await _resolve_related_alerts(db, device.id, "SNMP Trap: 接口流量中断", iface)
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 接口流量恢复",
            severity="info",
            message=f"接口 {iface} 流量恢复 (已恢复 {resolved} 条告警)",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: ifFlowUp on {device.name} {iface}")

    # ============================================================
    # 6. 华为硬盘/存储 Trap
    # ============================================================

    elif trap_oid == HW_HARDDISK_OVERFLOW_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 硬盘空间溢出",
            severity="major",
            message="硬盘空间已满（hwHarddiskoverflow），可能影响日志记录",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.warning(f"Alert created: harddisk overflow on {device.name}")

    elif trap_oid == HW_HARDDISK_THRESHOLD_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 硬盘空间告警",
            severity="warning",
            message="硬盘空间达到阈值（hwHarddiskReachThreshold）",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: harddisk threshold on {device.name}")

    elif trap_oid == HW_HARDDISK_OK_OID:
        resolved = await _resolve_related_alerts(db, device.id, "SNMP Trap: 硬盘空间%")
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 硬盘空间恢复",
            severity="info",
            message=f"硬盘空间恢复正常 (已恢复 {resolved} 条告警)",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: harddisk OK on {device.name}")

    # ============================================================
    # 7. 华为通用告警 Trap
    # ============================================================

    elif trap_oid == HW_RISING_ALARM_OID:
        extra = extract_threshold_info(trap.varbinds)
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 上升告警",
            severity="major",
            message=f"监测值超过上升阈值（pririsingAlarm）{' (' + extra + ')' if extra else ''}",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: rising alarm on {device.name}")

    elif trap_oid == HW_FALLING_ALARM_OID:
        resolved = await _resolve_related_alerts(db, device.id, "SNMP Trap: 上升告警")
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 下降告警",
            severity="info",
            message=f"监测值低于下降阈值，告警恢复 (已恢复 {resolved} 条告警)",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: falling alarm on {device.name}")

    # ============================================================
    # 8. 路由协议 Trap
    # ============================================================

    elif trap_oid == BGP_BACKWARD_OID:
        peer = _get_varbind_value(trap.varbinds, "bgpPeerRemoteAddr", "1.3.6.1.2.1.15.3.1.7") or "unknown"
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: BGP邻居断开",
            severity="major",
            message=f"BGP 邻居 {peer} 状态回退（bgpBackwardTransition）",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.warning(f"Alert created: BGP backward on {device.name} peer={peer}")

    elif trap_oid == BGP_ESTABLISHED_OID:
        peer = _get_varbind_value(trap.varbinds, "bgpPeerRemoteAddr", "1.3.6.1.2.1.15.3.1.7") or "unknown"
        resolved = await _resolve_related_alerts(db, device.id, "SNMP Trap: BGP邻居断开", peer)
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: BGP邻居建立",
            severity="info",
            message=f"BGP 邻居 {peer} 已建立 (已恢复 {resolved} 条告警)",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: BGP established on {device.name} peer={peer}")

    elif trap_oid == OSPF_NBR_STATE_CHANGE_OID:
        nbr = _get_varbind_value(trap.varbinds, "ospfNbrIpAddr", "1.3.6.1.2.1.14.10.1.1") or "unknown"
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: OSPF邻居状态变化",
            severity="warning",
            message=f"OSPF 邻居 {nbr} 状态发生变化",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: OSPF neighbor state change on {device.name} nbr={nbr}")

    elif trap_oid == OSPF_IF_STATE_CHANGE_OID:
        iface = extract_interface_info(trap.varbinds)
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: OSPF接口状态变化",
            severity="warning",
            message=f"OSPF 接口 {iface} 状态发生变化",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: OSPF if state change on {device.name} {iface}")

    elif trap_oid == VRRP_NEW_MASTER_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: VRRP主备切换",
            severity="minor",
            message="VRRP 发生主备切换，本机成为新的 Master",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: VRRP new master on {device.name}")

    elif trap_oid == VRRP_AUTH_FAILURE_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: VRRP认证失败",
            severity="warning",
            message="VRRP 认证失败，可能存在配置错误或安全威胁",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.warning(f"Alert created: VRRP auth failure on {device.name}")

    # ============================================================
    # 9. 实体配置变更 / LLDP
    # ============================================================

    elif trap_oid == ENTITY_CONFIG_CHANGE_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: 实体配置变更",
            severity="minor",
            message="设备实体配置发生变化（entConfigChange）",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: entity config change on {device.name}")

    elif trap_oid == LLDP_TABLES_CHANGE_OID:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap: LLDP邻居表变更",
            severity="info",
            message="LLDP 远程邻居表发生变化（lldpRemTablesChange），拓扑可能变更",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: LLDP tables change on {device.name}")

    # ============================================================
    # 10. 未知 Trap
    # ============================================================

    else:
        alert = Alert(
            device_id=device.id,
            rule_name="SNMP Trap",
            severity="warning",
            message=f"收到未知 Trap: {trap.trap_oid}",
            status="active",
            triggered_at=now,
        )
        db.add(alert)
        logger.info(f"Alert created: unknown trap {trap.trap_oid} from {device.name}")

    await db.commit()
    return {"status": "ok", "device": device.name, "trap_oid": trap_oid}


@router.get("/supported")
async def list_supported_traps():
    """列出系统支持的 Trap 类型"""
    return {
        "standard": [
            {"oid": COLD_START_OID, "name": "coldStart", "severity": "major", "desc": "设备冷启动"},
            {"oid": WARM_START_OID, "name": "warmStart", "severity": "minor", "desc": "设备热启动"},
            {"oid": LINK_DOWN_OID, "name": "linkDown", "severity": "critical", "desc": "接口 DOWN"},
            {"oid": LINK_UP_OID, "name": "linkUp", "severity": "info", "desc": "接口 UP (自动恢复)"},
            {"oid": AUTH_FAILURE_OID, "name": "authenticationFailure", "severity": "warning", "desc": "SNMP 认证失败"},
        ],
        "huawei_entity": [
            {"oid": HW_TEMP_THRESHOLD_OID, "name": "温度超限", "severity": "critical"},
            {"oid": HW_VOLTAGE_LOW_OID, "name": "电压过低", "severity": "critical"},
            {"oid": HW_VOLTAGE_HIGH_OID, "name": "电压过高", "severity": "critical"},
            {"oid": HW_CPU_THRESHOLD_OID, "name": "CPU超限", "severity": "major"},
            {"oid": HW_MEM_THRESHOLD_OID, "name": "内存超限", "severity": "major"},
            {"oid": HW_CPU_NORMAL_OID, "name": "CPU恢复正常", "severity": "info"},
            {"oid": HW_BOARD_ABNORMAL_OID, "name": "单板异常", "severity": "critical"},
            {"oid": HW_BOARD_NORMAL_OID, "name": "单板恢复", "severity": "info"},
            {"oid": HW_PORT_ABNORMAL_OID, "name": "端口异常", "severity": "critical"},
            {"oid": HW_PORT_NORMAL_OID, "name": "端口恢复", "severity": "info"},
            {"oid": HW_OPER_DISABLED_OID, "name": "实体禁用", "severity": "critical"},
            {"oid": HW_OPER_ENABLED_OID, "name": "实体启用", "severity": "info"},
        ],
        "huawei_system": [
            {"oid": HW_RELOAD_OID, "name": "设备重启", "severity": "critical"},
            {"oid": HW_CLOCK_CHANGED_OID, "name": "时钟变更", "severity": "minor"},
            {"oid": HW_SLAVE_SWITCH_OK_OID, "name": "主备切换成功", "severity": "minor"},
            {"oid": HW_SLAVE_SWITCH_FAIL_OID, "name": "主备切换失败", "severity": "critical"},
            {"oid": HW_CFG_FILE_ERROR_OID, "name": "配置文件错误", "severity": "critical"},
            {"oid": HW_IMAGE_ERROR_OID, "name": "系统镜像错误", "severity": "critical"},
        ],
        "huawei_stack": [
            {"oid": HW_STACK_LINK_DOWN_OID, "name": "堆叠链路断开", "severity": "critical"},
            {"oid": HW_STACK_LINK_UP_OID, "name": "堆叠链路恢复", "severity": "info"},
            {"oid": HW_STACK_RESTART_OID, "name": "堆叠系统重启", "severity": "critical"},
            {"oid": HW_STACK_MEMBER_LEAVE_OID, "name": "堆叠成员离开", "severity": "critical"},
            {"oid": HW_STACK_SWITCHOVER_OID, "name": "堆叠主备倒换", "severity": "minor"},
            {"oid": HW_STACK_MEMBER_ADD_OID, "name": "堆叠成员加入", "severity": "info"},
        ],
        "huawei_interface": [
            {"oid": HW_IF_FLOW_DOWN_OID, "name": "接口流量中断", "severity": "critical"},
            {"oid": HW_IF_FLOW_UP_OID, "name": "接口流量恢复", "severity": "info"},
        ],
        "huawei_storage": [
            {"oid": HW_HARDDISK_OVERFLOW_OID, "name": "硬盘空间溢出", "severity": "major"},
            {"oid": HW_HARDDISK_THRESHOLD_OID, "name": "硬盘空间告警", "severity": "warning"},
            {"oid": HW_HARDDISK_OK_OID, "name": "硬盘空间恢复", "severity": "info"},
        ],
        "routing_protocols": [
            {"oid": BGP_BACKWARD_OID, "name": "BGP邻居断开", "severity": "major"},
            {"oid": BGP_ESTABLISHED_OID, "name": "BGP邻居建立", "severity": "info"},
            {"oid": OSPF_NBR_STATE_CHANGE_OID, "name": "OSPF邻居状态变化", "severity": "warning"},
            {"oid": OSPF_IF_STATE_CHANGE_OID, "name": "OSPF接口状态变化", "severity": "warning"},
            {"oid": VRRP_NEW_MASTER_OID, "name": "VRRP主备切换", "severity": "minor"},
            {"oid": VRRP_AUTH_FAILURE_OID, "name": "VRRP认证失败", "severity": "warning"},
        ],
        "other": [
            {"oid": ENTITY_CONFIG_CHANGE_OID, "name": "实体配置变更", "severity": "minor"},
            {"oid": LLDP_TABLES_CHANGE_OID, "name": "LLDP邻居表变更", "severity": "info"},
            {"oid": HW_RISING_ALARM_OID, "name": "上升告警", "severity": "major"},
            {"oid": HW_FALLING_ALARM_OID, "name": "下降告警(恢复)", "severity": "info"},
        ],
    }
