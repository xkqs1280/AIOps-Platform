"""Server-rendered management UI.

This keeps the browser client deliberately small: the application logic remains
in FastAPI and the pages call the existing REST API with native ``fetch``.
"""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


WEB_DIR = Path(__file__).parent / "web_ui"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
router = APIRouter(include_in_schema=False)

NAV_ITEMS = [
    ("/", "总览"), ("/devices", "设备管理"), ("/alerts", "告警管理"),
    ("/topology", "拓扑发现"), ("/config-backup", "配置备份"),
    ("/inspection", "H3C 巡检"), ("/lifecycle", "生命周期"),
    ("/security", "安全监控"), ("/compliance", "等保合规"),
]


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {"nav_items": NAV_ITEMS})


@router.get("/devices", response_class=HTMLResponse)
@router.get("/alerts", response_class=HTMLResponse)
@router.get("/topology", response_class=HTMLResponse)
@router.get("/config-backup", response_class=HTMLResponse)
@router.get("/inspection", response_class=HTMLResponse)
@router.get("/lifecycle", response_class=HTMLResponse)
@router.get("/security", response_class=HTMLResponse)
@router.get("/compliance", response_class=HTMLResponse)
async def resource_page(request: Request):
    pages = {
        "devices": ("设备管理", "/devices"),
        "alerts": ("告警管理", "/alerts"),
        "topology": ("拓扑发现", "/topology"),
        "config-backup": ("配置备份", "/config-backups"),
        "inspection": ("H3C 巡检", "/inspections"),
        "lifecycle": ("生命周期", "/lifecycle/reminders"),
        "security": ("安全监控", "/security/external/latest"),
        "compliance": ("等保合规", "/compliance/status"),
    }
    # The route is explicit rather than a catch-all so /health, /docs and
    # future top-level API endpoints stay reachable.
    page = request.url.path.strip("/")
    title, endpoint = pages[page]
    return templates.TemplateResponse(
        request, "resource.html", {"nav_items": NAV_ITEMS, "title": title, "endpoint": endpoint}
    )


def setup_web_ui(app):
    """Attach the Python-owned web UI without affecting the REST API."""
    app.mount("/web-static", StaticFiles(directory=str(WEB_DIR / "static")), name="web-static")
    app.include_router(router)
