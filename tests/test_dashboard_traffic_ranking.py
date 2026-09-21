# -*- coding: utf-8 -*-
"""监控大屏「接口流量 TOP10」排名口径回归测试。

背景：该面板原先叫「带宽利用率 TOP10」，口径是**每台设备取利用率最高的那个接口**、
按利用率百分比降序。问题很明显——利用率是「接口自身带宽的占用比」：
1G 口跑 800M 显示 80% 上榜，10G 口跑 3G 只显示 30% 落榜，大屏上看不到真正的
大流量链路。

2026-09-21 改为**按接口收发总流量**（in_rate + out_rate，bps）排名，维度也从
「每设备一条」改为「全平台所有接口各自一条」。

这里锁定三条契约：
  1. 排序依据是 total_rate，不是 bandwidth_usage（否则这次改动等于没改）；
  2. LACP 聚合成员口必须被排除（其计数器累计聚合层流量，上榜即数字虚高）；
  3. 同一台设备的多个接口可以同时上榜（接口维度，不再按设备去重）。
"""


def _iface(name, in_rate, out_rate, speed=10_000_000_000, lacp=False):
    """构造 collect_interface_traffic 的返回项（字段与真实服务保持一致）。"""
    in_util = round(in_rate / speed * 100, 2) if speed else 0.0
    out_util = round(out_rate / speed * 100, 2) if speed else 0.0
    return {
        "ifindex": name,
        "name": name,
        "speed": speed,
        "in_rate": in_rate,
        "out_rate": out_rate,
        "in_util": in_util,
        "out_util": out_util,
        "max_util": max(in_util, out_util),
        "speed_source": "ifSpeed",
        "counter_64": True,
        "actual_interval": 3.0,
        "lacp_member": lacp,
        "util_capped": False,
    }


def _patch_collect(monkeypatch, per_ip: dict[str, list[dict]]):
    """把 SNMP 采集替换成按 IP 返回预设接口列表的假实现。"""
    from app.services import interface_traffic_service as svc

    async def fake_collect(ip, community="aiops", sample_interval=3.0, timeout=8):
        return list(per_ip.get(ip, []))

    monkeypatch.setattr(svc, "collect_interface_traffic", fake_collect)


def _reset_cache():
    from app.routers import dashboard

    dashboard._BW_CACHE["data"] = []
    dashboard._BW_CACHE["ts"] = 0.0


async def test_sorted_by_total_rate_not_utilization(db, device_factory, monkeypatch):
    """核心契约：按总流量排，不按利用率排。

    构造一组「利用率高但流量小」与「利用率低但流量大」的接口，断言大流量排前面。
    """
    from app.routers import dashboard

    await device_factory(name="SW-A", ip="10.0.0.1")
    _patch_collect(monkeypatch, {
        "10.0.0.1": [
            # 10G 口跑 3G（利用率 30%）—— 总流量 3.0G，应排第一
            _iface("Ten-GE1/0/1", 2_000_000_000, 1_000_000_000),
            # 1G 口跑 800M（利用率 80%）—— 总流量 0.8G，应排第二
            _iface("GE1/0/2", 800_000_000, 0, speed=1_000_000_000),
        ],
    })
    _reset_cache()

    await dashboard._collect_bandwidth(db)
    data = dashboard._BW_CACHE["data"]

    assert [d["total_rate"] for d in data] == [3_000_000_000, 800_000_000]
    assert data[0]["interface"] == "Ten-GE1/0/1"
    # 反证：按利用率排的话 GE1/0/2（80%）会排第一 —— 这正是本次要修掉的行为
    assert data[0]["bandwidth_usage"] < data[1]["bandwidth_usage"]


async def test_ranking_is_per_interface_not_per_device(db, device_factory, monkeypatch):
    """接口维度：同一台设备的多个接口可以同时上榜。"""
    from app.routers import dashboard

    await device_factory(name="SW-A", ip="10.0.0.1")
    _patch_collect(monkeypatch, {
        "10.0.0.1": [
            _iface("GE1/0/1", 5_000_000_000, 0),
            _iface("GE1/0/2", 3_000_000_000, 0),
            _iface("GE1/0/3", 1_000_000_000, 0),
        ],
    })
    _reset_cache()

    await dashboard._collect_bandwidth(db)
    data = dashboard._BW_CACHE["data"]

    assert len(data) == 3
    assert {d["name"] for d in data} == {"SW-A"}
    assert [d["interface"] for d in data] == ["GE1/0/1", "GE1/0/2", "GE1/0/3"]


async def test_lacp_member_never_on_board(db, device_factory, monkeypatch):
    """LACP 聚合成员口必须剔除：其 ifIn/OutOctets 累计聚合层流量，会虚高数倍。"""
    from app.routers import dashboard

    await device_factory(name="SW-A", ip="10.0.0.1")
    _patch_collect(monkeypatch, {
        "10.0.0.1": [
            # 成员口流量看着最大，但不可信，必须排除
            _iface("GE1/0/1", 9_000_000_000, 0, lacp=True),
            _iface("GE1/0/2", 1_000_000_000, 0),
        ],
    })
    _reset_cache()

    await dashboard._collect_bandwidth(db)
    data = dashboard._BW_CACHE["data"]

    assert [d["interface"] for d in data] == ["GE1/0/2"]
    assert all(not d["interface"].startswith("GE1/0/1") for d in data)


async def test_multi_device_and_zero_traffic_kept(db, device_factory, monkeypatch):
    """多设备合并排序；零流量接口不丢弃（前端据此区分「链路空闲」与「暂无数据」）。"""
    from app.routers import dashboard

    await device_factory(name="SW-A", ip="10.0.0.1")
    await device_factory(name="SW-B", ip="10.0.0.2")
    _patch_collect(monkeypatch, {
        "10.0.0.1": [_iface("GE1/0/1", 100_000_000, 0), _iface("GE1/0/2", 0, 0)],
        "10.0.0.2": [_iface("GE2/0/1", 2_000_000_000, 0)],
    })
    _reset_cache()

    await dashboard._collect_bandwidth(db)
    data = dashboard._BW_CACHE["data"]

    assert [d["name"] for d in data] == ["SW-B", "SW-A", "SW-A"]
    assert data[-1]["total_rate"] == 0
    assert data[-1]["interface"] == "GE1/0/2"


async def test_collect_failure_does_not_break_others(db, device_factory, monkeypatch):
    """单台设备不可达（SNMP 超时抛异常）时，其余设备仍要正常上榜。"""
    from app.routers import dashboard

    await device_factory(name="SW-BAD", ip="10.0.0.9")
    await device_factory(name="SW-OK", ip="10.0.0.1")

    from app.services import interface_traffic_service as svc

    async def fake_collect(ip, community="aiops", sample_interval=3.0, timeout=8):
        if ip == "10.0.0.9":
            raise TimeoutError("SNMP 读取接口信息失败")
        return [_iface("GE1/0/1", 500_000_000, 0)]

    monkeypatch.setattr(svc, "collect_interface_traffic", fake_collect)
    _reset_cache()

    await dashboard._collect_bandwidth(db)
    data = dashboard._BW_CACHE["data"]

    assert [d["name"] for d in data] == ["SW-OK"]
