from datetime import datetime
from pydantic import BaseModel, Field


class AlertRuleBase(BaseModel):
    name: str = Field(..., max_length=128)
    metric: str = Field(..., max_length=64)
    condition: str = Field(..., max_length=32)
    threshold: float
    duration: int = 300
    severity: str = Field(..., max_length=16)
    enabled: bool = True
    description: str | None = None


class AlertRuleCreate(AlertRuleBase):
    pass


class AlertRuleUpdate(BaseModel):
    name: str | None = None
    metric: str | None = None
    condition: str | None = None
    threshold: float | None = None
    duration: int | None = None
    severity: str | None = None
    enabled: bool | None = None
    description: str | None = None


class AlertRuleResponse(AlertRuleBase):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


class AlertResponse(BaseModel):
    id: int
    device_id: int
    device_name: str | None = None
    rule_name: str | None = None
    severity: str
    message: str
    status: str
    triggered_at: datetime
    resolved_at: datetime | None = None

    model_config = {"from_attributes": True}


class AlertListResponse(BaseModel):
    total: int
    items: list[AlertResponse]


class AlertStats(BaseModel):
    critical: int = 0
    major: int = 0
    minor: int = 0
    warning: int = 0
    total_active: int = 0
