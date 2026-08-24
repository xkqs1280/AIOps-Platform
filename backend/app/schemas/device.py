from datetime import datetime
import ipaddress

from pydantic import BaseModel, Field, field_validator


def _validate_ip(value: str) -> str:
    """校验 IPv4 / IPv6 地址格式，不合法直接抛 ValueError。"""
    ip = value.strip()
    if not ip:
        raise ValueError("IP 地址不能为空")
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        raise ValueError(f"IP 地址格式不正确：{value}")
    return ip


class DeviceBase(BaseModel):
    name: str = Field(..., max_length=128)
    ip: str = Field(..., max_length=45)
    vendor: str | None = None
    model: str | None = None
    serial_number: str | None = None
    snmp_version: str = "v2c"
    snmp_community: str | None = None
    mgmt_protocol: str | None = "ssh"
    mgmt_port: int | None = 22
    mgmt_username: str | None = None
    mgmt_password: str | None = None
    device_type: str | None = None
    group_name: str | None = None
    location: str | None = None
    warranty_expire: datetime | None = None
    eos_date: datetime | None = None
    eol_date: datetime | None = None

    @field_validator("ip")
    @classmethod
    def _check_ip(cls, v: str) -> str:
        return _validate_ip(v)


class DeviceCreate(DeviceBase):
    pass


class DeviceUpdate(BaseModel):
    name: str | None = None
    ip: str | None = None
    vendor: str | None = None
    model: str | None = None
    serial_number: str | None = None
    snmp_version: str | None = None
    snmp_community: str | None = None
    mgmt_protocol: str | None = None
    mgmt_port: int | None = None
    mgmt_username: str | None = None
    mgmt_password: str | None = None
    device_type: str | None = None
    group_name: str | None = None
    location: str | None = None
    warranty_expire: datetime | None = None
    eos_date: datetime | None = None
    eol_date: datetime | None = None
    status: str | None = None

    @field_validator("ip")
    @classmethod
    def _check_ip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_ip(v)


class DeviceResponse(BaseModel):
    """Public device representation. Never return connection secrets."""
    id: int
    name: str
    ip: str
    vendor: str | None = None
    model: str | None = None
    serial_number: str | None = None
    snmp_version: str | None = None
    mgmt_protocol: str | None = None
    mgmt_port: int | None = None
    device_type: str | None = None
    group_name: str | None = None
    location: str | None = None
    warranty_expire: datetime | None = None
    eos_date: datetime | None = None
    eol_date: datetime | None = None
    status: str
    cpu_usage: float | None = None
    memory_usage: float | None = None
    temperature: float | None = None
    last_seen: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class DeviceListResponse(BaseModel):
    total: int
    items: list[DeviceResponse]


class BatchDeviceDeleteRequest(BaseModel):
    """批量删除设备请求：指定 device_ids，或 delete_all 全删。"""
    device_ids: list[int] = Field(default_factory=list)
    delete_all: bool = False


# === 设备发现 ===

class DeviceDiscoverRequest(BaseModel):
    ips: list[str] = Field(..., min_length=1)
    snmp_community: str = Field("aiops", max_length=128)

    @field_validator("ips")
    @classmethod
    def _check_ips(cls, v: list[str]) -> list[str]:
        for ip in v:
            _validate_ip(ip)
        return v


class DiscoveredDevice(BaseModel):
    ip: str
    name: str | None = None
    vendor: str | None = None
    model: str | None = None
    device_type: str | None = None
    sys_descr: str | None = None
    already_managed: bool = False


class DeviceDiscoverResponse(BaseModel):
    total: int
    discovered: list[DiscoveredDevice]
    already_managed_count: int


class DeviceBatchCreate(BaseModel):
    devices: list[DeviceCreate]
