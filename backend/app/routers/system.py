# -*- coding: utf-8 -*-
"""系统升级 API 路由：版本查询 / 一键升级 / 进度查询 / 回滚

- GET    /api/v1/system/version             当前版本与构建信息
- POST   /api/v1/system/upgrade             上传升级包并开始升级（multipart）
- GET    /api/v1/system/upgrade/status      升级进度/状态（服务重启后仍可查询）
- POST   /api/v1/system/upgrade/rollback    回滚到升级前的备份

安全：所有接口仅管理员可用（升级会停服/替换文件，属于高危操作）。
"""
import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.routers.auth import admin_only
from app.services import upgrade_service
from app.services.audit_service import get_client_ip, record_audit
from app.version import APP_BUILD_TIME, APP_NAME, APP_VERSION

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system", tags=["系统升级"])

# 升级包大小上限：120MB（manifest + exe + 前端产物）
MAX_UPGRADE_PACKAGE_BYTES = 120 * 1024 * 1024
ALLOWED_EXTENSIONS = {".zip"}


@router.get("/version")
async def system_version():
    """当前平台版本与构建信息。"""
    return {
        "name": APP_NAME,
        "version": APP_VERSION,
        "build_time": APP_BUILD_TIME,
    }


@router.post("/upgrade")
async def start_upgrade(
    request: Request,
    file: UploadFile = File(...),
    actor: dict = Depends(admin_only),
    db: AsyncSession = Depends(get_db),
):
    """上传升级包并启动一键升级流程。"""
    ok, reason = upgrade_service.can_upgrade()
    if not ok:
        raise HTTPException(status_code=409, detail=reason)

    filename = (file.filename or "upgrade.zip").lower()
    if Path(filename).suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="升级包必须是 .zip 文件")

    # 流式写入临时文件（限制大小）
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    written = 0
    try:
        with open(tmp_fd, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPGRADE_PACKAGE_BYTES:
                    raise HTTPException(status_code=400, detail="升级包超过 120MB 上限")
                f.write(chunk)
    except HTTPException:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    except Exception as e:
        Path(tmp_path).unlink(missing_ok=True)
        logger.exception("Upload upgrade package failed")
        raise HTTPException(status_code=500, detail=f"上传失败：{e}")

    try:
        dest = upgrade_service.save_uploaded_zip(Path(tmp_path))
        status = upgrade_service.start_upgrade(dest)
    except ValueError as e:
        Path(tmp_path).unlink(missing_ok=True)
        await record_audit(db, actor, "upgrade", "upgrade", f"升级包校验失败：{e}",
                           get_client_ip(request))
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        Path(tmp_path).unlink(missing_ok=True)
        await record_audit(db, actor, "upgrade", "upgrade", f"升级启动失败：{e}",
                           get_client_ip(request))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    from_version = status.get("from_version", "?")
    to_version = status.get("to_version", "?")
    await record_audit(db, actor, "upgrade", "upgrade",
                       f"开始系统升级 {from_version} → {to_version}（包：{filename}）",
                       get_client_ip(request))
    return JSONResponse({"ok": True, "message": "升级已开始，可轮询状态接口查看进度", "status": status})


@router.get("/upgrade/status")
async def upgrade_status(_: dict = Depends(admin_only)):
    """当前升级进度（读状态文件，服务重启后仍有效）。"""
    return upgrade_service.get_status()


@router.post("/upgrade/rollback")
async def rollback(
    request: Request,
    actor: dict = Depends(admin_only),
    db: AsyncSession = Depends(get_db),
):
    """回滚到升级前备份（停服 → 恢复 exe/frontend/.env → 重启）。"""
    s = upgrade_service.load_state()
    if not s.get("rollback_available") and not (upgrade_service.get_upgrade_root() / "backup").exists():
        raise HTTPException(status_code=409, detail="没有可回滚的备份")
    try:
        status = upgrade_service.request_rollback()
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await record_audit(db, actor, "upgrade", "rollback", "开始回滚系统升级", get_client_ip(request))
    return JSONResponse({"ok": True, "message": "回滚已开始，可轮询状态接口查看进度", "status": status})
