import asyncio
import logging
import sys
from contextlib import asynccontextmanager

# Windows: psycopg async requires SelectorEventLoop
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select

from app.config import settings
from app.database import async_session, init_db
from app.version import APP_BUILD_TIME, APP_NAME, APP_VERSION
from app.models.user import User
from app.services.auth_service import create_access_token, decode_access_token, hash_password, require_password_strength
from app.routers import devices, alerts, alert_rules, topology, dashboard
from app.routers import baselines, predictions, health, lifecycle
from app.routers import syslog, security, compliance
from app.routers import config_backups
from app.routers import traps
from app.routers import metrics
from app.routers import inspections
from app.routers import business_monitor
from app.routers import terminal
from app.routers import license
from app.routers import auth
from app.routers import settings as settings_router
from app.routers import ai as ai_router
from app.routers import system

logger = logging.getLogger(__name__)


async def _backup_scheduler():
    """后台定时备份调度器，每60秒检查一次到期任务"""
    from app.services.backup_service import run_scheduled_backups
    # 启动后等待30秒，确保DB就绪
    await asyncio.sleep(30)
    while True:
        try:
            await run_scheduled_backups()
        except Exception as e:
            logger.error(f"Backup scheduler error: {e}")
        await asyncio.sleep(60)


async def _backup_cleanup_loop():
    """每日清理：6 个月（180 天）前的所有备份记录 + 1 个月（30 天）前的备份失败记录。启动后先执行一次再按天循环。"""
    from app.services.backup_service import cleanup_old_backups
    await asyncio.sleep(30)
    while True:
        try:
            deleted = await cleanup_old_backups(days=180, failed_days=30)
            if deleted:
                logger.info(f"Backup cleanup: removed {deleted} expired records")
        except Exception as e:
            logger.error(f"Backup cleanup error: {e}")
        await asyncio.sleep(86400)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # 数据库结构迁移（升级后自动执行，幂等；失败会阻止启动，提示回滚）
    try:
        from app.migrations import run_migrations
        applied = await run_migrations()
        if applied:
            logger.info("Database migrations applied: %s", applied)
    except Exception as e:
        logger.critical("Database migration failed: %s. Upgrade is not fully applied.", e)
        raise
    # 存量明文设备凭据一次性加密（启用加密密钥后的迁移，幂等）
    try:
        from app.services.credential_service import encrypt_existing_device_secrets
        await encrypt_existing_device_secrets()
    except Exception as e:
        logger.error(f"Encrypt existing credentials failed: {e}")
    async with async_session() as session:
        first_user = (await session.execute(select(User.id).limit(1))).scalar_one_or_none()
        if first_user is None:
            if settings.BOOTSTRAP_ADMIN_USERNAME and settings.BOOTSTRAP_ADMIN_PASSWORD:
                if not settings.ALLOW_INSECURE_BOOTSTRAP:
                    require_password_strength(settings.BOOTSTRAP_ADMIN_PASSWORD)
                else:
                    logger.warning("Insecure bootstrap administrator password is enabled; never use this outside an isolated test environment.")
                session.add(User(username=settings.BOOTSTRAP_ADMIN_USERNAME, password_hash=hash_password(settings.BOOTSTRAP_ADMIN_PASSWORD), role="admin"))
                await session.commit()
                logger.warning("Bootstrap administrator created: %s", settings.BOOTSTRAP_ADMIN_USERNAME)
            else:
                logger.error("No users exist. Set BOOTSTRAP_ADMIN_USERNAME and BOOTSTRAP_ADMIN_PASSWORD before first startup.")
    # 启动定时备份调度器
    scheduler_task = asyncio.create_task(_backup_scheduler())
    logger.info("Backup scheduler started")
    # 启动设备可达性检测服务（每5秒探测，3次失败判定离线）
    from app.services.health_check_service import health_check_loop
    health_check_task = asyncio.create_task(health_check_loop())
    logger.info("Device health check service started")
    # 启动真实指标采集服务（每60秒 SNMP 采集 CPU/内存/温度）
    from app.services.metrics_collector import metrics_collect_loop
    metrics_task = asyncio.create_task(metrics_collect_loop())
    logger.info("Metrics collector service started")
    # 启动外部威胁情报采集服务（每30分钟抓取 FireHOL + ip-api 地理标注）
    from app.services.external_threat_service import threat_collect_loop
    threat_task = asyncio.create_task(threat_collect_loop())
    logger.info("External threat collector service started")
    # 启动重要业务监控服务（每5分钟 ping 探测终端，离线告警）
    from app.services.business_monitor_service import start_business_monitor
    biz_monitor_task = await start_business_monitor()
    # 启动配置备份清理（每日删除 6 个月前的备份记录）
    cleanup_task = asyncio.create_task(_backup_cleanup_loop())
    logger.info("Backup cleanup service started")
    yield
    scheduler_task.cancel()
    health_check_task.cancel()
    metrics_task.cancel()
    threat_task.cancel()
    biz_monitor_task.cancel()
    cleanup_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass
    try:
        await health_check_task
    except asyncio.CancelledError:
        pass
    try:
        await metrics_task
    except asyncio.CancelledError:
        pass
    try:
        await threat_task
    except asyncio.CancelledError:
        pass
    try:
        await biz_monitor_task
    except asyncio.CancelledError:
        pass
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title=APP_NAME,
    description="网络及安全设备 7×24 智能监控与故障预测平台",
    version=APP_VERSION,
    lifespan=lifespan,
    # 生产环境关闭 API 文档，避免 /openapi.json 匿名泄露端点结构（安全基线）
    docs_url="/docs" if settings.API_DOCS_ENABLED else None,
    redoc_url="/redoc" if settings.API_DOCS_ENABLED else None,
    openapi_url="/openapi.json" if settings.API_DOCS_ENABLED else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    path = request.url.path
    api_prefix = settings.API_PREFIX
    # CORS 预检请求（OPTIONS）不携带凭据，直接放行，由 CORSMiddleware 处理响应头
    if request.method == "OPTIONS":
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response
    # /license/* 免登录（登录页/授权页需在未登录或锁定时获取授权状态）
    protected = path.startswith(f"{api_prefix}/") and not path.startswith(f"{api_prefix}/auth/") and not path.startswith(f"{api_prefix}/license/")
    ingest_path = path in {f"{api_prefix}/traps", f"{api_prefix}/syslog"}
    if settings.AUTH_ENABLED and protected:
        ingest_key = request.headers.get("X-Ingest-Key")
        if ingest_path and settings.INGEST_API_KEY and ingest_key == settings.INGEST_API_KEY:
            pass
        else:
            token = request.cookies.get("access_token")
            authorization = request.headers.get("Authorization", "")
            if authorization.startswith("Bearer "):
                token = authorization[7:]
            # WebSocket 客户端（如设备 CLI 终端）可能无法携带 cookie，支持 query 参数
            if not token:
                token = request.query_params.get("token")
            if not token:
                return JSONResponse({"detail": "请先登录"}, status_code=401)
            try:
                identity = decode_access_token(token)
            except Exception:
                return JSONResponse({"detail": "登录已失效"}, status_code=401)
            async with async_session() as session:
                account = (await session.execute(select(User).where(User.username == identity["sub"]))).scalar_one_or_none()
            if not account or not account.is_active:
                return JSONResponse({"detail": "用户已停用或不存在"}, status_code=401)
            identity["role"] = account.role
            if request.method not in {"GET", "HEAD", "OPTIONS"} and identity["role"] == "viewer":
                return JSONResponse({"detail": "当前角色没有写入权限"}, status_code=403)
            if request.method == "DELETE" and identity["role"] != "admin":
                return JSONResponse({"detail": "删除操作需要管理员权限"}, status_code=403)
    # 授权锁定检查：未激活 / 测试版到期 → 锁定平台，仅授权页与登录放行
    from app.services.license_service import is_locked
    if path.startswith(f"{api_prefix}/") and not path.startswith(f"{api_prefix}/license/") and not path.startswith(f"{api_prefix}/auth/"):
        if await is_locked():
            return JSONResponse(
                {"code": 403002, "detail": "平台未授权或授权已到期，请前往授权管理页处理"},
                status_code=403,
            )
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

# 注册路由
app.include_router(devices.router, prefix=settings.API_PREFIX)
app.include_router(terminal.router, prefix=settings.API_PREFIX)
app.include_router(alerts.router, prefix=settings.API_PREFIX)
app.include_router(alert_rules.router, prefix=settings.API_PREFIX)
app.include_router(topology.router, prefix=settings.API_PREFIX)
app.include_router(dashboard.router, prefix=settings.API_PREFIX)
app.include_router(baselines.router, prefix=settings.API_PREFIX)
app.include_router(predictions.router, prefix=settings.API_PREFIX)
app.include_router(health.router, prefix=settings.API_PREFIX)
app.include_router(lifecycle.router, prefix=settings.API_PREFIX)
app.include_router(syslog.router, prefix=settings.API_PREFIX)
app.include_router(security.router, prefix=settings.API_PREFIX)
app.include_router(compliance.router, prefix=settings.API_PREFIX)
app.include_router(config_backups.router, prefix=settings.API_PREFIX)
app.include_router(traps.router, prefix=settings.API_PREFIX)
app.include_router(metrics.router, prefix=settings.API_PREFIX)
app.include_router(inspections.router, prefix=settings.API_PREFIX)
app.include_router(business_monitor.router, prefix=settings.API_PREFIX)
app.include_router(license.router, prefix=settings.API_PREFIX)
app.include_router(auth.router, prefix=settings.API_PREFIX)
app.include_router(settings_router.router, prefix=settings.API_PREFIX)
app.include_router(ai_router.router, prefix=settings.API_PREFIX)
app.include_router(system.router, prefix=settings.API_PREFIX)


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/login", include_in_schema=False)
async def login_page():
    """返回 SPA（前端 Login.vue）实现「登录即解锁语音」。无前端产物时回退内置登录页。"""
    _idx = _os.path.join(_DIST_DIR, "index.html")
    if _os.path.isfile(_idx):
        return FileResponse(_idx, headers=_NO_CACHE_HEADERS)
    return HTMLResponse(LEGACY_LOGIN_HTML)


# 无前端产物时 /login 的内置登录页兜底（独立 HTML，登录成功整页跳转）
LEGACY_LOGIN_HTML = """<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>AIOps 登录</title><style>body{font-family:system-ui;background:#0b1220;color:#e8eef8;display:grid;place-items:center;height:100vh;margin:0}form{background:#15233b;padding:32px;border-radius:12px;width:320px;text-align:center}img.logo{width:56px;height:56px;display:block;margin:0 auto 8px}input,button{box-sizing:border-box;width:100%;padding:11px;margin:7px 0;border-radius:6px;border:1px solid #405573}button{background:#1687a7;color:white;border:0}#err{color:#ffabab}#lic{max-width:320px;margin-bottom:12px;padding:10px 12px;border-radius:8px;font-size:13px;text-align:center;display:none}.lic-red{background:#3b1520;border:1px solid #7f2a3c;color:#ffb0b0}.lic-orange{background:#3b2d12;border:1px solid #7f6a24;color:#ffd9a0}</style><div id='lic'></div><form id='f'><img class='logo' src='/logo.svg' alt='AIOps'><h2>AIOps 平台登录</h2><input id='u' placeholder='用户名' required><input id='p' type='password' placeholder='密码' required><button>登录</button><p id='err'></p></form><script>fetch('/api/v1/license/status').then(r=>r.json()).then(s=>{const d=document.getElementById('lic');if(!s.enabled)return;let cls='',txt='';if(!s.activated){cls='lic-red';txt='⚠ 平台未授权，登录后请前往「授权管理」激活（联系邮箱 x1280455974@163.com）'}else if(s.locked){cls='lic-red';txt='⚠ 平台授权已到期并锁定，请前往「授权管理」激活（联系邮箱 x1280455974@163.com）'}else if(!s.permanent&&s.days_left!==null&&s.days_left<=30){cls='lic-orange';txt='⚠ 平台授权将于 '+(s.expires_at||'').slice(0,10)+' 到期（剩余 '+s.days_left+' 天），请及时续期（联系邮箱 x1280455974@163.com）'}if(txt){d.className=cls;d.textContent=txt;d.style.display='block'}}).catch(()=>{});f.onsubmit=async e=>{e.preventDefault();let r=await fetch('/api/v1/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u.value,password:p.value})});if(r.ok)location='/';else err.textContent=(await r.json()).detail||'登录失败'}</script></html>"""


# ---- 简易部署模式：若 backend 上级存在 frontend/dist，则由后端同端口托管前端（SPA）----
# 使 Windows 一键部署包可单进程、单端口运行（http://IP:8000）；不影响常规前后端分离部署。
import os as _os
from fastapi.responses import FileResponse, JSONResponse

# SPA 入口 index.html 禁止缓存（assets 带 hash 可长缓存）：
# 否则浏览器启发式缓存旧 index.html，前端发版后用户看到的仍是旧版本。
_NO_CACHE_HEADERS = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}


def _resolve_dist_dir() -> str:
    """定位前端构建产物目录，按优先级：
    1. exe 同级 frontend/dist（PyInstaller 打包，前端外置、可独立升级）
    2. PyInstaller 内置包内 frontend/dist（--add-data 方式）
    3. 源码模式：backend/app/main.py 上跳 3 级到项目根/frontend/dist
    """
    if getattr(sys, "frozen", False):
        _exe_dir = _os.path.dirname(_os.path.abspath(sys.executable))
        _external = _os.path.join(_exe_dir, "frontend", "dist")
        if _os.path.isdir(_external):
            return _external
        _bundled = _os.path.join(getattr(sys, "_MEIPASS", ""), "frontend", "dist")
        if _bundled and _os.path.isdir(_bundled):
            return _bundled
        return _external
    return _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), "frontend", "dist")


_DIST_DIR = _resolve_dist_dir()


# ---- 移动端前端（H5 / App WebView）：frontend/mobile_dist 独立构建产物，/mobile/ 路径托管 ----
def _resolve_mobile_dist_dir() -> str:
    if getattr(sys, "frozen", False):
        _exe_dir = _os.path.dirname(_os.path.abspath(sys.executable))
        _external = _os.path.join(_exe_dir, "frontend", "mobile_dist")
        if _os.path.isdir(_external):
            return _external
        _bundled = _os.path.join(getattr(sys, "_MEIPASS", ""), "frontend", "mobile_dist")
        if _bundled and _os.path.isdir(_bundled):
            return _bundled
        return _external
    return _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), "frontend", "mobile_dist")


_MOBILE_DIST_DIR = _resolve_mobile_dist_dir()


@app.get("/mobile", include_in_schema=False)
@app.get("/mobile/{full_path:path}", include_in_schema=False)
async def _mobile_static(full_path: str = ""):
    """移动端前端静态托管（/mobile/ 前缀），SPA 回退 index.html。"""
    if not _os.path.isdir(_MOBILE_DIST_DIR):
        return JSONResponse({"detail": "Mobile frontend not built"}, status_code=404)
    if (
        ".." in full_path or _os.path.isabs(full_path)
        or full_path.startswith("api/") or full_path.startswith("docs")
        or full_path.startswith("openapi") or full_path.startswith("redoc")
    ):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    candidate = _os.path.join(_MOBILE_DIST_DIR, full_path) if full_path else ""
    if full_path and _os.path.isfile(candidate):
        return FileResponse(candidate)
    index = _os.path.join(_MOBILE_DIST_DIR, "index.html")
    if _os.path.isfile(index):
        return FileResponse(index, headers=_NO_CACHE_HEADERS)
    return JSONResponse({"detail": "Not Found"}, status_code=404)


@app.get("/")
async def root(request: Request):
    """有前端构建产物时首页直接返回前端页面；否则返回服务信息（兼容纯 API 部署）。"""
    if settings.AUTH_ENABLED:
        token = request.cookies.get("access_token")
        if not token:
            return RedirectResponse("/login", status_code=303)
        try:
            decode_access_token(token)
        except Exception:
            # token 存在但已失效（如密钥变更/会话过期）：清除后跳登录，避免空白页
            response = RedirectResponse("/login", status_code=303)
            response.delete_cookie("access_token")
            return response
    _idx = _os.path.join(_DIST_DIR, "index.html")
    if _os.path.isfile(_idx):
        return FileResponse(_idx, headers=_NO_CACHE_HEADERS)
    return {"status": "ok", "service": "AIOps Platform", "version": APP_VERSION}


@app.get("/{full_path:path}", include_in_schema=False)
async def _spa_static(full_path: str):
    """静态文件优先返回；SPA 路由回退 index.html；API 未匹配仍返回 JSON 404。"""
    if not _os.path.isdir(_DIST_DIR):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    if (
        ".." in full_path or _os.path.isabs(full_path)
        or full_path.startswith("api/") or full_path.startswith("docs")
        or full_path.startswith("openapi") or full_path.startswith("redoc")
    ):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    candidate = _os.path.join(_DIST_DIR, full_path)
    if full_path and _os.path.isfile(candidate):
        return FileResponse(candidate)
    index = _os.path.join(_DIST_DIR, "index.html")
    if _os.path.isfile(index):
        return FileResponse(index, headers=_NO_CACHE_HEADERS)
    return JSONResponse({"detail": "Not Found"}, status_code=404)
