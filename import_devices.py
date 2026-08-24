"""批量导入设备到 AIOps 平台（通过 API，凭据自动加密）"""
import httpx

BASE = "http://localhost:8000/api/v1"

# 用户提供的设备清单（示例数据，请替换为你自己的设备；ID 为参考，导入时由数据库自增）
devices = [
    # name, ip, vendor, model, device_type, status, snmp_community, ssh_user, ssh_pass
    dict(name="Router-01", ip="10.0.0.1", vendor="H3C", model="MSR36-20", device_type="路由器", status="offline", snmp_community="public", ssh_user=None, ssh_pass=None),
    dict(name="Switch-01", ip="10.0.0.2", vendor="H3C", model="S6850", device_type="交换机", status="offline", snmp_community="public", ssh_user=None, ssh_pass=None),
    dict(name="FW-01", ip="10.0.0.3", vendor="H3C", model="SecPath F1090", device_type="防火墙", status="offline", snmp_community="public", ssh_user=None, ssh_pass=None),
]

def main():
    with httpx.Client(base_url=BASE, timeout=30) as client:
        # 登录
        r = client.post("/auth/login", json={"username": "admin", "password": "admin"})
        print(f"[login] {r.status_code}")
        if r.status_code != 200:
            print(r.text)
            return

        # 查询现有设备
        r = client.get("/devices", params={"page_size": 300})
        if r.status_code == 200:
            existing = r.json()
            print(f"[existing] total={existing['total']}")
            for d in existing["items"]:
                print(f"  id={d['id']} {d['name']} {d['ip']} status={d['status']}")

        # 批量导入
        payload = {"devices": []}
        for d in devices:
            item = {
                "name": d["name"],
                "ip": d["ip"],
                "vendor": d["vendor"],
                "model": d["model"],
                "device_type": d["device_type"],
                "status": d["status"],
                "snmp_version": "v2c",
                "snmp_community": d["snmp_community"],
            }
            if d["ssh_user"]:
                item.update({
                    "mgmt_protocol": "ssh",
                    "mgmt_port": 22,
                    "mgmt_username": d["ssh_user"],
                    "mgmt_password": d["ssh_pass"],
                })
            else:
                item["mgmt_protocol"] = None
                item["mgmt_port"] = None
            payload["devices"].append(item)

        r = client.post("/devices/batch", json=payload)
        print(f"\n[batch create] {r.status_code}")
        if r.status_code in (200, 201):
            data = r.json()
            print(f"created={data['total']}")
            for d in data["items"]:
                print(f"  id={d['id']} {d['name']} {d['ip']} status={d['status']}")
        else:
            print(r.text)

if __name__ == "__main__":
    main()
