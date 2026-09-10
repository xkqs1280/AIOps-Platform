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
    # 判定模式：threshold=静态阈值 / baseline=同时段动态基线
    mode: str = Field("threshold", max_length=16)
    # 基线模式：|z| 超过该倍数判为异常
    baseline_sigma: float = 3.0
    # 基线模式：upper=仅正偏离 / lower=仅负偏离 / both=双向
    baseline_direction: str = Field("upper", max_length=8)


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
    mode: str | None = None
    baseline_sigma: float | None = None
    baseline_direction: str | None = None


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
    # status: active / resolved / suppressed
    status: str
    triggered_at: datetime
    resolved_at: datetime | None = None
    # 收敛信息
    flap_count: int = 0
    suppressed_by_device_id: int | None = None
    suppressed_by_name: str | None = None
    suppress_reason: str | None = None
    aggregated_count: int = 0

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
    # 被收敛抑制的告警数（不计入 total_active）
    suppressed: int = 0
