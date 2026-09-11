# -*- coding: utf-8 -*-
"""编辑设备页「测试连接」：凭据回落与探测结论汇总。

覆盖三类真实事故面：
  1. 编辑页不回显密钥（DeviceResponse 不含 snmp_community / mgmt_password），
     若不做「留空→沿用库中凭据」的回落，用户不改密码就永远测不通；
  2. 用户改了密码/Community 时必须优先用表单值，不能被库里的旧值覆盖；
  3. 探测失败的措辞要能定位问题（认证失败 / 端口被拒 / 超时），而不是抛底层异常。
"""
import pytest
from pydantic import ValidationError

from app.schemas.device import DeviceTestRequest


# ---------------------------------------------------------------------------
# 请求体校验
# ---------------------------------------------------------------------------

def test_request_validates_ip():
    with pytest.raises(ValidationError):
        DeviceTestRequest(ip="999.1.1.1")
    ok = DeviceTestRequest(ip="10.0.0.1")
    assert ok.ip == "10.0.0.1"
    assert ok.mgmt_protocol == "ssh"
    assert ok.timeout == 8.0


def test_request_rejects_out_of_range_timeout():
    with pytest.raises(ValidationError):
        DeviceTestRequest(ip="10.0.0.1", timeout=0.5)
    with pytest.raises(ValidationError):
        DeviceTestRequest(ip="10.0.0.1", timeout=60)


# ---------------------------------------------------------------------------
# 服务层：探测结果汇总
# ---------------------------------------------------------------------------

async def test_service_summary_all_ok(monkeypatch):
    from app.services import device_test_service as svc

    async def fake_ssh(*a, **k):
        return {"ok": True, "message": "SSH 登录成功", "detail": "已通过口令认证"}

    async def fake_snmp(*a, **k):
        return {"ok": True, "message": "SNMP 可通"}

    monkeypatch.setattr(svc, "_test_ssh", fake_ssh)
    monkeypatch.setattr(svc, "_test_snmp", fake_snmp)

    res = await svc.test_device_connection(ip="10.0.0.1", username="admin",
                                           password="pw", community="public")
    assert res["ok"] is True
    assert res["summary"] == "SSH 登录与 SNMP 均正常"
    assert [i["target"] for i in res["items"]] == ["ssh", "snmp"]
    assert all(i["ok"] for i in res["items"])


async def test_service_summary_partial_failures(monkeypatch):
    from app.services import device_test_service as svc

    async def ok_ssh(*a, **k):
        return {"ok": True, "message": "SSH 登录成功"}

    async def bad_snmp(*a, **k):
        return {"ok": False, "message": "SNMP 无响应"}

    monkeypatch.setattr(svc, "_test_ssh", ok_ssh)
    monkeypatch.setattr(svc, "_test_snmp", bad_snmp)
    res = await svc.test_device_connection(ip="10.0.0.1", username="admin",
                                           password="pw", community="public")
    assert res["ok"] is False
    assert res["summary"] == "SSH 登录成功，SNMP 不通"

    # 反向：SNMP 通、登录失败
    async def bad_ssh(*a, **k):
        return {"ok": False, "message": "SSH 认证失败：用户名或密码不正确"}

    async def ok_snmp(*a, **k):
        return {"ok": True, "message": "SNMP 可通"}

    monkeypatch.setattr(svc, "_test_ssh", bad_ssh)
    monkeypatch.setattr(svc, "_test_snmp", ok_snmp)
    res = await svc.test_device_connection(ip="10.0.0.1", username="admin",
                                           password="pw", community="public")
    assert res["ok"] is False
    assert res["summary"] == "SNMP 可通，SSH 登录失败"


async def test_service_requires_credentials_without_network_io(monkeypatch):
    """缺用户名 / 密码时直接给结论，不去连设备（避免把空口令发到设备上）。"""
    from app.services import device_test_service as svc

    called = False

    async def never(*a, **k):
        nonlocal called
        called = True
        return {"ok": True, "message": "should not be called"}

    monkeypatch.setattr(svc, "_test_ssh", never)

    async def fake_snmp(*a, **k):
        return {"ok": True, "message": "SNMP 可通"}

    monkeypatch.setattr(svc, "_test_snmp", fake_snmp)

    res = await svc.test_device_connection(ip="10.0.0.1", username="", password="",
                                           community="public")
    assert called is False
    assert res["items"][0]["ok"] is False
    assert "未填写用户名" in res["items"][0]["message"]


async def test_service_rejects_unknown_protocol():
    from app.services import device_test_service as svc
    res = await svc.test_device_connection(ip="10.0.0.1", protocol="rdp",
                                           username="u", password="p")
    assert res["ok"] is False
    assert "不支持的管理协议" in res["summary"]


async def test_service_flags_snmp_v3_as_skipped(monkeypatch):
    """平台用 v2c（community）采集，v3 不具备可比对的凭据 → 明确说明而非报"不通"。"""
    from app.services import device_test_service as svc

    async def ok_ssh(*a, **k):
        return {"ok": True, "message": "SSH 登录成功"}

    monkeypatch.setattr(svc, "_test_ssh", ok_ssh)
    res = await svc.test_device_connection(ip="10.0.0.1", username="admin", password="pw",
                                           snmp_version="v3", community="")
    snmp_item = res["items"][1]
    assert snmp_item["ok"] is False
    assert snmp_item["skipped"] is True
    assert "v3" in snmp_item["message"]


async def test_service_marks_saved_credential(monkeypatch):
    from app.services import device_test_service as svc

    async def fake_ssh(*a, **k):
        return {"ok": True, "message": "SSH 登录成功"}

    async def fake_snmp(*a, **k):
        return {"ok": True, "message": "SNMP 可通"}

    monkeypatch.setattr(svc, "_test_ssh", fake_ssh)
    monkeypatch.setattr(svc, "_test_snmp", fake_snmp)
    res = await svc.test_device_connection(ip="10.0.0.1", username="admin", password="pw",
                                           community="public",
                                           password_from_db=True, community_from_db=True)
    assert all(i["used_saved_credential"] for i in res["items"])


def test_network_error_classification():
    from app.services.device_test_service import _classify_network_error
    assert "端口被拒绝" in _classify_network_error(ConnectionRefusedError(), "SSH", 8)
    assert "超时" in _classify_network_error(TimeoutError(), "SSH", 8)
    assert "主机地址" in _classify_network_error(__import__("socket").gaierror(), "SSH", 8)


# ---------------------------------------------------------------------------
# 路由层：凭据回落
# ---------------------------------------------------------------------------

async def _call_router(db, monkeypatch, data, captured):
    import app.routers.devices as dev_router

    async def fake(**kw):
        captured.update(kw)
        return {"ok": True, "summary": "SSH 登录与 SNMP 均正常", "items": []}

    monkeypatch.setattr(dev_router, "test_device_connection", fake)
    return await dev_router.test_connection(
        data=data, user={"sub": "admin", "role": "admin"}, db=db)


async def test_router_falls_back_to_saved_credentials(db, device_factory, monkeypatch):
    dev = await device_factory(name="SW1", ip="10.0.0.1", mgmt_protocol="ssh", mgmt_port=22,
                               mgmt_username="admin", mgmt_password="savedpass",
                               snmp_community="savedcomm", snmp_version="v2c")
    captured: dict = {}
    # 编辑页表单里密钥为空（后端不回显），必须回落到库里的值
    res = await _call_router(db, monkeypatch, DeviceTestRequest(
        device_id=dev.id, ip="10.0.0.1", mgmt_username="", mgmt_password="",
        snmp_community="", mgmt_port=None), captured)

    assert captured["password"] == "savedpass"
    assert captured["password_from_db"] is True
    assert captured["community"] == "savedcomm"
    assert captured["community_from_db"] is True
    assert captured["username"] == "admin"
    assert captured["port"] == 22
    assert captured["protocol"] == "ssh"
    assert res.ok is True and res.ip == "10.0.0.1"


async def test_router_prefers_form_values(db, device_factory, monkeypatch):
    """表单里填了新密码 / 新 Community 时，必须用新值（测试"改完能不能通"）。"""
    dev = await device_factory(name="SW1", ip="10.0.0.1", mgmt_port=22,
                               mgmt_username="admin", mgmt_password="oldpass",
                               snmp_community="oldcomm")
    captured: dict = {}
    await _call_router(db, monkeypatch, DeviceTestRequest(
        device_id=dev.id, ip="10.0.0.1", mgmt_protocol="telnet", mgmt_port=23,
        mgmt_username="newuser", mgmt_password="newpass",
        snmp_community="newcomm"), captured)

    assert captured["password"] == "newpass"
    assert captured["password_from_db"] is False
    assert captured["community"] == "newcomm"
    assert captured["community_from_db"] is False
    assert captured["username"] == "newuser"
    assert captured["port"] == 23
    assert captured["protocol"] == "telnet"


async def test_router_without_device_id_passes_values_through(db, monkeypatch):
    """新增设备场景（无 device_id）：不做回落，空凭据原样透传。"""
    captured: dict = {}
    await _call_router(db, monkeypatch, DeviceTestRequest(
        ip="10.0.0.9", mgmt_username="u", mgmt_password="p",
        snmp_community="c"), captured)
    assert captured["password"] == "p"
    assert captured["password_from_db"] is False
    assert captured["community_from_db"] is False
