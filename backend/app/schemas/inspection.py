from datetime import datetime
from pydantic import BaseModel, Field


class InspectionDeviceResultResponse(BaseModel):
    id: int
    device_id: int
    device_name: str
    device_ip: str
    status: str
    error_message: str | None = None
    created_at: datetime
    completed_at: datetime | None = None
    # 巡检解析结果（版本/型号/CPU/内存/检查项等），供前端详情页展示
    parsed_data: dict | None = None

    model_config = {"from_attributes": True}


class InspectionTaskResponse(BaseModel):
    id: int
    name: str
    status: str
    device_ids: list[int]
    total_devices: int
    success_count: int
    failed_count: int
    excel_path: str | None = None
    word_path: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    model_config = {"from_attributes": True}


class InspectionTaskDetailResponse(InspectionTaskResponse):
    device_results: list[InspectionDeviceResultResponse] = []


class InspectionTaskListResponse(BaseModel):
    total: int
    items: list[InspectionTaskResponse]


class InspectionCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    device_ids: list[int] = Field(..., min_length=1)
