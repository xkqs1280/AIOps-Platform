from datetime import datetime
from pydantic import BaseModel, Field


# === Config Backup Schemas ===

class ConfigBackupResponse(BaseModel):
    id: int
    device_id: int
    backup_type: str
    config_hash: str | None = None
    file_size: int = 0
    line_count: int = 0
    status: str
    error_message: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class ConfigBackupDetail(ConfigBackupResponse):
    config_content: str


class ConfigBackupListResponse(BaseModel):
    total: int
    items: list[ConfigBackupResponse]


class DiffResponse(BaseModel):
    backup1_id: int
    backup2_id: int
    diff: list[dict]


# === Backup Schedule Schemas ===

class BackupScheduleCreate(BaseModel):
    device_id: int | None = None
    is_all_devices: bool = False
    frequency: str = Field("daily", pattern="^(daily|weekly|monthly)$")
    day_of_week: int | None = Field(None, ge=0, le=6)
    day_of_month: int | None = Field(None, ge=1, le=28)
    hour: int = Field(2, ge=0, le=23)
    minute: int = Field(0, ge=0, le=59)
    enabled: bool = True


class BackupScheduleUpdate(BaseModel):
    frequency: str | None = Field(None, pattern="^(daily|weekly|monthly)$")
    day_of_week: int | None = Field(None, ge=0, le=6)
    day_of_month: int | None = Field(None, ge=1, le=28)
    hour: int | None = Field(None, ge=0, le=23)
    minute: int | None = Field(None, ge=0, le=59)
    enabled: bool | None = None


class BackupScheduleResponse(BaseModel):
    id: int
    device_id: int | None = None
    is_all_devices: bool = False
    device_name: str | None = None
    frequency: str
    day_of_week: int | None = None
    day_of_month: int | None = None
    hour: int
    minute: int
    enabled: bool
    last_backup_at: datetime | None = None
    next_backup_at: datetime | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class BackupScheduleListResponse(BaseModel):
    total: int
    items: list[BackupScheduleResponse]
