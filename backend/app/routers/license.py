"""平台授权管理 API — 机器指纹 / 授权状态 / 激活"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.routers.auth import current_user
from app.services.audit_service import record_audit

# 保证 license_info 表在启动建表时注册到 metadata（init_db 依赖模型已 import）
from app.models.license import LicenseInfo  # noqa: F401
from app.services.license_service import (
    activate_license,
    get_license_status,
    get_machine_fingerprint,
)

router = APIRouter(prefix="/license", tags=["授权管理"])


class ActivateRequest(BaseModel):
    license_code: str


@router.get("/fingerprint")
async def license_fingerprint():
    """返回本机机器指纹（供厂商生成激活码）"""
    return {"fingerprint": get_machine_fingerprint()}


@router.get("/status")
async def license_status():
    """返回当前授权状态"""
    return await get_license_status()


@router.post("/activate")
async def license_activate(body: ActivateRequest, actor: dict = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """输入激活码激活（离线验签 + 指纹比对）"""
    result = await activate_license(body.license_code)
    await record_audit(db, actor, "license", "activate", "执行授权激活")
    return result
