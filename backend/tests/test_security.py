from app.schemas.device import DeviceResponse
from app.services.credential_service import protect_device_secrets


def test_device_response_does_not_include_credentials():
    assert "mgmt_password" not in DeviceResponse.model_fields
    assert "snmp_community" not in DeviceResponse.model_fields


def test_secret_protection_is_a_noop_without_migration_key(monkeypatch):
    # 显式模拟"未配置 CREDENTIAL_ENCRYPTION_KEY"的部署（勿依赖运行环境 .env）
    monkeypatch.setattr(
        "app.services.credential_service.settings.CREDENTIAL_ENCRYPTION_KEY", ""
    )
    values = protect_device_secrets({"name": "edge", "mgmt_password": "secret"})
    assert values["mgmt_password"] == "secret"

