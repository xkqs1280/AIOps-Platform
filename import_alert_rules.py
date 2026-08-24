"""从告警规则导出 xlsx 导入告警规则到 AIOps 平台（通过 API）。

字段映射参照 backend/seed_data.py 的权威值：
- 监控指标 -> metric: SNMP 可达性=snmp_reachable, CPU 利用率=cpu_usage, 内存利用率=memory_usage,
  接口运行状态=if_oper_status, 带宽利用率=bandwidth_usage, 接口错误率=if_error_rate,
  设备运行时间(重启检测)=sys_uptime, 磁盘使用率=disk_usage, 温度=temperature
- 条件 -> condition: 大于=gt, 等于=eq, 变化检测(重启)=delta
- 严重级别 -> severity: 严重=critical, 重要=major, 次要=minor, 警告=warning
- 是否启用 -> enabled: 启用=True
"""
import httpx

BASE = "http://localhost:8000/api/v1"

# (规则名称, 监控指标, 条件, 阈值, 持续时长, 严重级别, 说明)
# 数据来自 告警规则导出-20260807.xlsx「告警规则」sheet（10 条）
RULES = [
    ("设备不可达", "snmp_reachable", "eq", 0, 180, "critical", "SNMP 连续 3 次无响应"),
    ("CPU 过载", "cpu_usage", "gt", 90, 300, "major", "CPU 利用率 >90% 持续 5 分钟"),
    ("内存不足", "memory_usage", "gt", 85, 300, "major", "内存利用率 >85%"),
    ("接口 Down", "if_oper_status", "eq", 2, 60, "major", "接口运行状态为 Down"),
    ("带宽超限", "bandwidth_usage", "gt", 80, 600, "minor", "接口带宽利用率 >90% 持续 10 分钟"),
    ("接口错误率高", "if_error_rate", "gt", 1, 300, "minor", "接口错误率 >1%"),
    ("设备重启", "sys_uptime", "delta", 0, 60, "critical", "设备运行时间重置，疑似重启"),
    ("磁盘不足", "disk_usage", "gt", 90, 300, "minor", "磁盘使用率 >90%"),
    ("温度过高", "temperature", "gt", 65, 300, "minor", "设备温度超过 65°C"),
    ("CPU 预警", "cpu_usage", "gt", 80, 600, "warning", "CPU 利用率 >80% 持续 10 分钟"),
]


def main():
    with httpx.Client(base_url=BASE, timeout=30) as client:
        # 登录
        r = client.post("/auth/login", json={"username": "admin", "password": "admin"})
        print(f"[login] {r.status_code}")
        if r.status_code != 200:
            print(r.text)
            return

        # 查询现有规则
        r = client.get("/alert-rules")
        existing = r.json()
        print(f"[existing] {len(existing)} 条规则")
        existing_names = {rule["name"] for rule in existing}

        created = 0
        skipped = 0
        for name, metric, condition, threshold, duration, severity, desc in RULES:
            if name in existing_names:
                print(f"  [skip] {name} 已存在")
                skipped += 1
                continue
            payload = {
                "name": name,
                "metric": metric,
                "condition": condition,
                "threshold": float(threshold),
                "duration": int(duration),
                "severity": severity,
                "enabled": True,
                "description": desc,
            }
            r = client.post("/alert-rules", json=payload)
            if r.status_code in (200, 201):
                created += 1
                print(f"  [ok] {name} -> {r.json()['id']}")
            else:
                print(f"  [FAIL] {name}: {r.status_code} {r.text}")

        print(f"\n结果：新增 {created} 条，跳过 {skipped} 条")


if __name__ == "__main__":
    main()
