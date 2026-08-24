"""
种子数据脚本：初始化演示数据（设备、告警规则、模拟告警）
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone

# Windows: psycopg async requires SelectorEventLoop
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import select

from app.database import async_session, init_db, engine, Base
from app.models.device import Device
from app.models.alert import AlertRule, Alert

# 当前时间
now = datetime.now(timezone.utc)
tz_8 = timezone(timedelta(hours=8))


async def seed():
    await init_db()

    async with async_session() as db:
        # 检查是否已有数据
        result = await db.execute(select(Device).limit(1))
        if result.scalar_one_or_none():
            print("数据库已有数据，跳过种子初始化")
            return

        # === 设备数据 ===
        devices_data = [
            Device(
                name="核心交换机-01", ip="10.0.0.1", vendor="H3C", model="S10508",
                serial_number="H3C-S10508-001", snmp_version="v2c", snmp_community="public",
                device_type="switch", group_name="核心层", location="中心机房 A-01",
                warranty_expire=datetime(2027, 6, 15, tzinfo=tz_8),
                eos_date=datetime(2028, 12, 31, tzinfo=tz_8),
                eol_date=datetime(2030, 12, 31, tzinfo=tz_8),
                status="online", cpu_usage=42.5, memory_usage=58.3, temperature=45.0,
                last_seen=now,
            ),
            Device(
                name="核心交换机-02", ip="10.0.0.2", vendor="H3C", model="S10508",
                serial_number="H3C-S10508-002", snmp_version="v2c", snmp_community="public",
                device_type="switch", group_name="核心层", location="中心机房 A-02",
                warranty_expire=datetime(2027, 6, 15, tzinfo=tz_8),
                eos_date=datetime(2028, 12, 31, tzinfo=tz_8),
                eol_date=datetime(2030, 12, 31, tzinfo=tz_8),
                status="online", cpu_usage=38.2, memory_usage=55.1, temperature=43.0,
                last_seen=now,
            ),
            Device(
                name="汇聚交换机-01", ip="10.0.1.1", vendor="华为", model="S6730-H48X6C",
                serial_number="HW-S6730-001", snmp_version="v2c", snmp_community="public",
                device_type="switch", group_name="汇聚层", location="楼层机房 3F",
                warranty_expire=datetime(2028, 3, 20, tzinfo=tz_8),
                status="online", cpu_usage=65.8, memory_usage=72.4, temperature=52.0,
                last_seen=now,
            ),
            Device(
                name="汇聚交换机-02", ip="10.0.1.2", vendor="华为", model="S6730-H48X6C",
                serial_number="HW-S6730-002", snmp_version="v2c", snmp_community="public",
                device_type="switch", group_name="汇聚层", location="楼层机房 5F",
                warranty_expire=datetime(2028, 3, 20, tzinfo=tz_8),
                status="online", cpu_usage=60.1, memory_usage=68.7, temperature=50.0,
                last_seen=now,
            ),
            Device(
                name="接入交换机-01", ip="10.0.2.1", vendor="H3C", model="S5130S-52P-EI",
                serial_number="H3C-S5130-001", snmp_version="v2c", snmp_community="public",
                device_type="switch", group_name="接入层", location="3F 办公区",
                status="online", cpu_usage=25.3, memory_usage=40.1, temperature=38.0,
                last_seen=now,
            ),
            Device(
                name="接入交换机-02", ip="10.0.2.2", vendor="H3C", model="S5130S-52P-EI",
                serial_number="H3C-S5130-002", snmp_version="v2c", snmp_community="public",
                device_type="switch", group_name="接入层", location="5F 办公区",
                status="online", cpu_usage=30.7, memory_usage=45.2, temperature=39.0,
                last_seen=now,
            ),
            Device(
                name="接入交换机-03", ip="10.0.2.3", vendor="华为", model="S5735-L48P4X",
                serial_number="HW-S5735-001", snmp_version="v2c", snmp_community="public",
                device_type="switch", group_name="接入层", location="7F 研发区",
                warranty_expire=datetime(2025, 12, 1, tzinfo=tz_8),
                status="warning", cpu_usage=88.4, memory_usage=82.9, temperature=62.0,
                last_seen=now,
            ),
            Device(
                name="接入交换机-04", ip="10.0.2.4", vendor="锐捷", model="RG-S2910-48GT4XS-E",
                serial_number="RG-S2910-001", snmp_version="v2c", snmp_community="public",
                device_type="switch", group_name="接入层", location="1F 大厅",
                status="online", cpu_usage=20.1, memory_usage=35.5, temperature=36.0,
                last_seen=now,
            ),
            Device(
                name="防火墙-01", ip="10.0.0.254", vendor="深信服", model="AF-2000-FH",
                serial_number="SGF-AF2000-001", snmp_version="v2c", snmp_community="public",
                device_type="firewall", group_name="安全", location="中心机房 A-03",
                warranty_expire=datetime(2027, 9, 10, tzinfo=tz_8),
                status="online", cpu_usage=45.6, memory_usage=60.2, temperature=48.0,
                last_seen=now,
            ),
            Device(
                name="AC 控制器-01", ip="10.0.3.1", vendor="H3C", model="WX3540H",
                serial_number="H3C-WX3540-001", snmp_version="v2c", snmp_community="public",
                device_type="ac", group_name="无线", location="中心机房 B-01",
                status="online", cpu_usage=35.2, memory_usage=50.8, temperature=41.0,
                last_seen=now,
            ),
        ]

        for d in devices_data:
            db.add(d)
        await db.flush()

        # === 告警规则 ===
        rules_data = [
            AlertRule(name="设备不可达", metric="snmp_reachable", condition="eq", threshold=0, duration=180, severity="critical", description="SNMP 连续 3 次无响应"),
            AlertRule(name="CPU 过载", metric="cpu_usage", condition="gt", threshold=90, duration=300, severity="major", description="CPU 利用率 >90% 持续 5 分钟"),
            AlertRule(name="内存不足", metric="memory_usage", condition="gt", threshold=85, duration=300, severity="major", description="内存利用率 >85%"),
            AlertRule(name="接口 Down", metric="if_oper_status", condition="eq", threshold=2, duration=60, severity="major", description="接口运行状态为 Down"),
            AlertRule(name="带宽超限", metric="bandwidth_usage", condition="gt", threshold=90, duration=600, severity="minor", description="接口带宽利用率 >90% 持续 10 分钟"),
            AlertRule(name="接口错误率高", metric="if_error_rate", condition="gt", threshold=1, duration=300, severity="minor", description="接口错误率 >1%"),
            AlertRule(name="设备重启", metric="sys_uptime", condition="delta", threshold=0, duration=60, severity="critical", description="设备运行时间重置，疑似重启"),
            AlertRule(name="磁盘不足", metric="disk_usage", condition="gt", threshold=90, duration=300, severity="minor", description="磁盘使用率 >90%"),
            AlertRule(name="温度过高", metric="temperature", condition="gt", threshold=65, duration=300, severity="minor", description="设备温度超过 65°C"),
            AlertRule(name="CPU 预警", metric="cpu_usage", condition="gt", threshold=80, duration=600, severity="warning", description="CPU 利用率 >80% 持续 10 分钟"),
        ]

        for r in rules_data:
            db.add(r)
        await db.flush()

        # === 模拟告警 ===
        alerts_data = [
            Alert(device_id=7, rule_name="CPU 过载", severity="major", message="接入交换机-03 CPU 利用率 88.4%，超过阈值 90%（接近阈值）", status="active", triggered_at=now - timedelta(minutes=15)),
            Alert(device_id=7, rule_name="内存不足", severity="major", message="接入交换机-03 内存利用率 82.9%，接近阈值 85%", status="active", triggered_at=now - timedelta(minutes=10)),
            Alert(device_id=7, rule_name="温度过高", severity="minor", message="接入交换机-03 温度 62°C，接近阈值 65°C", status="active", triggered_at=now - timedelta(minutes=20)),
            Alert(device_id=1, rule_name="带宽超限", severity="minor", message="核心交换机-01 上行链路带宽利用率 92% 超过阈值 90%", status="active", triggered_at=now - timedelta(minutes=25)),
            Alert(device_id=3, rule_name="CPU 预警", severity="warning", message="汇聚交换机-01 CPU 利用率 65.8%，超过预警阈值 80%", status="active", triggered_at=now - timedelta(hours=1)),
            Alert(device_id=5, rule_name="接口 Down", severity="major", message="接入交换机-01 GigabitEthernet1/0/24 接口 Down", status="resolved", triggered_at=now - timedelta(hours=3), resolved_at=now - timedelta(hours=2, minutes=30)),
            Alert(device_id=1, rule_name="设备不可达", severity="critical", message="核心交换机-01 SNMP 连续 3 次无响应", status="resolved", triggered_at=now - timedelta(days=1), resolved_at=now - timedelta(hours=23)),
        ]

        for a in alerts_data:
            db.add(a)

        await db.commit()
        print(f"种子数据初始化完成：{len(devices_data)} 台设备, {len(rules_data)} 条告警规则, {len(alerts_data)} 条告警")


if __name__ == "__main__":
    asyncio.run(seed())
