from app.schemas.device import DeviceResponse
from app.services.credential_service import protect_device_secrets


def test_device_response_does_not_include_credentials():
    assert "mgmt_password" not in DeviceResponse.model_fields
    assert "snmp_community" not in DeviceResponse.model_fields


def test_secret_protection_is_a_noop_without_migration_key():
    values = protect_device_secrets({"name": "edge", "mgmt_password": "secret"})
    assert values["mgmt_password"] == "secret"
