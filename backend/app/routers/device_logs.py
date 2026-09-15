# -*- coding: utf-8 -*-
"""设备日志中心 API：日志查询 / 统计 / 导出 / HTTP 接入 / loghost 下发与回滚。

对应「设备日志中心 P0」：设备侧 syslog 经内置 UDP 接收器（``syslog_receiver``）入库，
本路由提供查询与运维操作能力。

几个刻意的设计：
  - **默认按平台接收时间 ``received_at`` 排序与过滤**，而不是设备侧时间 ``device_time``：
    设备时钟可能不准或未配时区（实测 H3C 出厂返回 UTC），用它排序会让顺序错乱。
    ``device_time`` 仅作为详情里的参考信息返回。
  - **关键字搜索对 ``%`` ``_`` ``\\`` 做转义**，否则用户搜 "100%" 会把全表带出来。
  - **loghost 下发是高危操作**：必须先 preview 看命令、apply 时显式 confirm，
    且全程先快照再下发后回验，可随时回滚；每次操作都落库留痕并写审计日志。
"""
import csv
import io
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.config import settings
from app.database import get_db
from app.models.device import Device
from app.models.device_log import DeviceLog, DeviceLogHostConfig
from app.routers.auth import admin_only, current_user
from app.services.audit_service import get_client_ip, record_audit
from app.services.device_log_service import (
    CATEGORY_LABELS,
    SEVERITY_LABELS,
    SEVERITY_NAMES,
)
from app.services.rate_limit import limit_ingest
from app.services import syslog_receiver
from app.services import device_loghost_service as loghost

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/device-logs", tags=["设备日志中心"])

MAX_PAGE_SIZE = 200


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------

def _like_pattern(raw: str) -> str:
    """把用户关键字转成 LIKE 模式，并转义 ``%`` / ``_`` / ``\\`` 通配符。

    否则用户搜 "100%" 会被当成通配符，把全部日志都带出来。
    """
    escaped = raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _parse_dt(value: str | None, field: str) -> datetime | None:
    """解析 ISO 时间参数（支持 ``Z`` 结尾与仅日期）。"""
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field} 时间格式不合法：{value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_severities(raw: str | None) -> list[int] | None:
    """解析级别过滤：逗号分隔的 0-7。"""
    if not raw:
        return None
    out: list[int] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"日志级别不合法：{part!r}（应为 0-7）")
        if not 0 <= n <= 7:
            raise HTTPException(status_code=422, detail=f"日志级别超出范围（0-7）：{n}")
        out.append(n)
    return out or None


def _build_filters(
    *, device_id: int | None, missing_device: bool, module: str | None,
    category: str | None, mnemonic: str | None, severities: list[int] | None,
    max_severity: int | None, keyword: str | None, interface: str | None,
    username: str | None, start: datetime | None, end: datetime | None,
) -> list:
    """构造 device_logs 的过滤条件（列表与统计共用，保证口径一致）。"""
    conds = []
    if device_id is not None:
        conds.append(DeviceLog.device_id == device_id)
    if missing_device:
        # 未纳管设备发来的日志（排查"有设备没纳管"的线索）
        conds.append(DeviceLog.device_id.is_(None))
    if module:
        conds.append(DeviceLog.module == module.strip().upper())
    if category:
        conds.append(DeviceLog.category == category.strip().lower())
    if mnemonic:
        conds.append(DeviceLog.mnemonic.ilike(_like_pattern(mnemonic.strip()), escape="\\"))
    if severities:
        conds.append(DeviceLog.severity.in_(severities))
    if max_severity is not None:
        conds.append(DeviceLog.severity <= max_severity)
    if interface:
        conds.append(DeviceLog.interface.ilike(_like_pattern(interface.strip()), escape="\\"))
    if username:
        conds.append(DeviceLog.username.ilike(_like_pattern(username.strip()), escape="\\"))
    if start is not None:
        conds.append(DeviceLog.received_at >= start)
    if end is not None:
        conds.append(DeviceLog.received_at <= end)
    if keyword and keyword.strip():
        pattern = _like_pattern(keyword.strip())
        dev_ids = select(Device.id).where(
            or_(Device.name.ilike(pattern, escape="\\"), Device.ip.ilike(pattern, escape="\\"))
        )
        conds.append(or_(
            DeviceLog.content.ilike(pattern, escape="\\"),
            DeviceLog.raw_log.ilike(pattern, escape="\\"),
            DeviceLog.mnemonic.ilike(pattern, escape="\\"),
            DeviceLog.module.ilike(pattern, escape="\\"),
            DeviceLog.interface.ilike(pattern, escape="\\"),
            DeviceLog.username.ilike(pattern, escape="\\"),
            DeviceLog.hostname.ilike(pattern, escape="\\"),
            DeviceLog.src_ip.ilike(pattern, escape="\\"),
            DeviceLog.device_id.in_(dev_ids),
        ))
    return conds


def _row_to_dict(log: DeviceLog) -> dict:
    dev = log.device
    return {
        "id": log.id,
        "device_id": log.device_id,
        "device_name": dev.name if dev else None,
        "device_ip": dev.ip if dev else None,
        "hostname": log.hostname,
        "module": log.module,
        "mnemonic": log.mnemonic,
        "severity": log.severity,
        "severity_name": log.severity_name,
        "severity_label": SEVERITY_LABELS.get(log.severity) if log.severity is not None else None,
        "category": log.category,
        "category_label": CATEGORY_LABELS.get(log.category or "", log.category),
        "interface": log.interface,
        "username": log.username,
        "src_ip": log.src_ip,
        "content": log.content,
        "device_time": log.device_time.isoformat() if log.device_time else None,
        "device_time_raw": log.device_time_raw,
        "received_at": log.received_at.isoformat() if log.received_at else None,
        "log_source": log.log_source,
    }


# ---------------------------------------------------------------------------
# 查询 / 统计
# ---------------------------------------------------------------------------

@router.get("")
async def list_device_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    device_id: int | None = Query(None, description="按设备 ID 过滤"),
    missing_device: bool = Query(False, description="只看未纳管设备（来源 IP 不在设备表）的日志"),
    module: str | None = Query(None, max_length=32, description="日志模块，如 IFNET / SHELL"),
    category: str | None = Query(None, max_length=16, description="分类 link/auth/config/protocol/system/security/other"),
    mnemonic: str | None = Query(None, max_length=64, description="助记符，模糊匹配"),
    severity: str | None = Query(None, max_length=32, description="级别过滤，逗号分隔 0-7"),
    max_severity: int | None = Query(None, ge=0, le=7, description="只看不高于该级别的日志"),
    interface: str | None = Query(None, max_length=128, description="接口名，模糊匹配"),
    username: str | None = Query(None, max_length=64, description="操作用户，模糊匹配"),
    keyword: str | None = Query(None, max_length=200, description="关键字：正文/原始报文/模块/接口/设备名/IP"),
    start: str | None = Query(None, description="起始时间（ISO，按接收时间）"),
    end: str | None = Query(None, description="结束时间（ISO，按接收时间）"),
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(current_user),
):
    """设备日志列表（默认按平台接收时间倒序）。"""
    conds = _build_filters(
        device_id=device_id, missing_device=missing_device, module=module,
        category=category, mnemonic=mnemonic, severities=_parse_severities(severity),
        max_severity=max_severity, keyword=keyword, interface=interface,
        username=username, start=_parse_dt(start, "start"), end=_parse_dt(end, "end"),
    )

    total = (await db.execute(
        select(func.count(DeviceLog.id)).where(*conds)
    )).scalar_one()

    rows = (await db.execute(
        select(DeviceLog)
        .options(joinedload(DeviceLog.device))
        .where(*conds)
        .order_by(DeviceLog.received_at.desc(), DeviceLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "items": [_row_to_dict(r) for r in rows],
    }


@router.get("/stats")
async def device_log_stats(
    hours: int | None = Query(None, ge=1, le=24 * 365, description="统计窗口（小时）；与 start/end 二选一"),
    start: str | None = Query(None, description="起始时间（ISO）"),
    end: str | None = Query(None, description="结束时间（ISO）"),
    device_id: int | None = Query(None),
    category: str | None = Query(None, max_length=16),
    keyword: str | None = Query(None, max_length=200),
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(current_user),
):
    """日志统计：总量 / 按级别 / 按分类 / 按模块 Top / 按设备 Top / 按小时趋势。"""
    if start or end:
        start_dt = _parse_dt(start, "start")
        end_dt = _parse_dt(end, "end")
    else:
        window = hours or 24
        start_dt = datetime.now(timezone.utc) - timedelta(hours=window)
        end_dt = None

    conds = _build_filters(
        device_id=device_id, missing_device=False, module=None, category=category,
        mnemonic=None, severities=None, max_severity=None, keyword=keyword,
        interface=None, username=None, start=start_dt, end=end_dt,
    )

    total = (await db.execute(select(func.count(DeviceLog.id)).where(*conds))).scalar_one()

    sev_rows = (await db.execute(
        select(DeviceLog.severity, func.count(DeviceLog.id))
        .where(*conds).group_by(DeviceLog.severity).order_by(DeviceLog.severity)
    )).all()
    by_severity = [
        {
            "severity": s,
            "name": SEVERITY_NAMES.get(s, "unknown") if s is not None else "unknown",
            "label": SEVERITY_LABELS.get(s, "未知") if s is not None else "未知",
            "count": c,
        }
        for s, c in sev_rows
    ]

    cat_rows = (await db.execute(
        select(DeviceLog.category, func.count(DeviceLog.id))
        .where(*conds).group_by(DeviceLog.category).order_by(func.count(DeviceLog.id).desc())
    )).all()
    by_category = [
        {"category": c, "label": CATEGORY_LABELS.get(c or "", c or "unknown"), "count": n}
        for c, n in cat_rows
    ]

    mod_rows = (await db.execute(
        select(DeviceLog.module, func.count(DeviceLog.id))
        .where(*conds, DeviceLog.module.is_not(None))
        .group_by(DeviceLog.module).order_by(func.count(DeviceLog.id).desc()).limit(12)
    )).all()
    by_module = [{"module": m, "count": n} for m, n in mod_rows]

    dev_rows = (await db.execute(
        select(DeviceLog.device_id, func.count(DeviceLog.id))
        .where(*conds, DeviceLog.device_id.is_not(None))
        .group_by(DeviceLog.device_id).order_by(func.count(DeviceLog.id).desc()).limit(10)
    )).all()
    dev_ids = [d for d, _ in dev_rows if d is not None]
    name_map: dict[int, tuple[str, str]] = {}
    if dev_ids:
        for did, dname, dip in (await db.execute(
            select(Device.id, Device.name, Device.ip).where(Device.id.in_(dev_ids))
        )).all():
            name_map[did] = (dname, dip)
    by_device = [
        {
            "device_id": d,
            "device_name": name_map.get(d, ("未知设备", ""))[0],
            "device_ip": name_map.get(d, ("", ""))[1],
            "count": n,
        }
        for d, n in dev_rows
    ]

    hour_expr = _hour_expr()
    hour_rows = (await db.execute(
        select(hour_expr.label("h"), func.count(DeviceLog.id))
        .where(*conds).group_by(hour_expr).order_by(hour_expr)
    )).all()
    by_hour = [{"hour": h, "count": n} for h, n in hour_rows]

    return {
        "window": {
            "start": start_dt.isoformat() if start_dt else None,
            "end": end_dt.isoformat() if end_dt else None,
        },
        "total": total,
        "by_severity": by_severity,
        "by_category": by_category,
        "by_module": by_module,
        "by_device": by_device,
        "by_hour": by_hour,
    }


def _hour_expr():
    """按小时分桶的时间表达式（PostgreSQL 与 SQLite 语法不同，按方言分支）。"""
    if settings.DATABASE_URL.startswith("postgresql"):
        return func.to_char(func.date_trunc("hour", DeviceLog.received_at), "YYYY-MM-DD HH24:00")
    return func.strftime("%Y-%m-%d %H:00", DeviceLog.received_at)


@router.get("/filters")
async def device_log_filters(
    hours: int = Query(24 * 7, ge=1, le=24 * 365),
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(current_user),
):
    """筛选项：可用模块、分类、有日志的设备（供前端下拉，避免硬编码）。"""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    mods = (await db.execute(
        select(DeviceLog.module, func.count(DeviceLog.id))
        .where(DeviceLog.received_at >= since, DeviceLog.module.is_not(None))
        .group_by(DeviceLog.module).order_by(func.count(DeviceLog.id).desc()).limit(80)
    )).all()
    devs = (await db.execute(
        select(Device.id, Device.name, Device.ip)
        .where(Device.id.in_(
            select(DeviceLog.device_id).where(
                DeviceLog.received_at >= since, DeviceLog.device_id.is_not(None)
            ).distinct()
        )).order_by(Device.name)
    )).all()
    return {
        "modules": [{"module": m, "count": n} for m, n in mods],
        "categories": [{"category": c, "label": CATEGORY_LABELS.get(c, c)} for c in CATEGORY_LABELS],
        "severities": [
            {"severity": s, "name": SEVERITY_NAMES[s], "label": SEVERITY_LABELS[s]}
            for s in sorted(SEVERITY_NAMES)
        ],
        "devices": [{"id": i, "name": n, "ip": ip} for i, n, ip in devs],
    }


@router.get("/receiver")
async def receiver_status(_user: dict = Depends(current_user)):
    """内置 syslog UDP 接收器状态（运行中 / 端口 / 绑定错误 / 丢弃计数）。"""
    return syslog_receiver.receiver_status()


@router.get("/export")
async def export_device_logs(
    device_id: int | None = Query(None),
    missing_device: bool = Query(False),
    module: str | None = Query(None, max_length=32),
    category: str | None = Query(None, max_length=16),
    mnemonic: str | None = Query(None, max_length=64),
    severity: str | None = Query(None, max_length=32),
    max_severity: int | None = Query(None, ge=0, le=7),
    interface: str | None = Query(None, max_length=128),
    username: str | None = Query(None, max_length=64),
    keyword: str | None = Query(None, max_length=200),
    start: str | None = Query(None),
    end: str | None = Query(None),
    limit: int = Query(50_000, ge=1, le=200_000, description="导出上限，防止一次拉爆内存"),
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(current_user),
):
    """按当前筛选条件导出 CSV（带 BOM，Excel 直接打开不乱码）。"""
    conds = _build_filters(
        device_id=device_id, missing_device=missing_device, module=module,
        category=category, mnemonic=mnemonic, severities=_parse_severities(severity),
        max_severity=max_severity, keyword=keyword, interface=interface,
        username=username, start=_parse_dt(start, "start"), end=_parse_dt(end, "end"),
    )
    header = [
        "接收时间(UTC)", "设备时间(原样)", "设备", "IP", "主机名", "模块", "助记符",
        "级别", "分类", "接口", "用户", "来源IP", "内容", "原始报文",
    ]

    async def _rows():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        yield "\ufeff" + buf.getvalue()

        offset = 0
        chunk = 2000
        written = 0
        while written < limit:
            rows = (await db.execute(
                select(DeviceLog)
                .options(joinedload(DeviceLog.device))
                .where(*conds)
                .order_by(DeviceLog.received_at.desc(), DeviceLog.id.desc())
                .offset(offset).limit(min(chunk, limit - written))
            )).scalars().all()
            if not rows:
                break
            buf = io.StringIO()
            w = csv.writer(buf)
            for r in rows:
                d = _row_to_dict(r)
                w.writerow([
                    d["received_at"], d["device_time_raw"] or d["device_time"], d["device_name"] or "",
                    d["device_ip"] or d["src_ip"] or "", d["hostname"] or "", d["module"] or "",
                    d["mnemonic"] or "", d["severity_name"] or "", d["category_label"] or "",
                    d["interface"] or "", d["username"] or "", d["src_ip"] or "",
                    (d["content"] or "")[:2000], (r.raw_log or "")[:4000],
                ])
            yield buf.getvalue()
            written += len(rows)
            offset += len(rows)

    fname = f"device_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        _rows(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ---------------------------------------------------------------------------
# HTTP 接入（外部转发组件 / TCP-only 设备的补充通道）
# ---------------------------------------------------------------------------

class IngestRequest(BaseModel):
    raw_log: str = Field(..., min_length=1, max_length=65535)
    source_ip: str | None = Field(None, max_length=45)


@router.post("/ingest")
async def ingest_device_log(
    body: IngestRequest,
    _: None = Depends(limit_ingest),
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(current_user),
):
    """把一条日志塞进接收队列（走与 UDP 完全相同的解析与入库路径）。

    用途：设备只支持 TCP syslog、需要 rsyslog/syslog-ng 中转时，由外部组件转发到这里。
    正常情况下设备直发 UDP 即可，无需本接口。

    限流与 ``/syslog``、``/traps`` 两个接入端点保持一致（``limit_ingest``，120 次/分/IP）：
    队列上限只能兜住「攒批层」，挡不住请求层突发，这里必须单独限。
    """
    if not body.raw_log.strip():
        raise HTTPException(status_code=400, detail="raw_log 不能为空")
    queued = syslog_receiver.handle_datagram(
        body.raw_log.encode("utf-8", errors="replace"),
        body.source_ip or "unknown",
        log_source="http",
    )
    if queued == 0:
        raise HTTPException(status_code=429, detail="接收队列已满或触发限流，请稍后重试")
    # 立即刷一次，保证接入方能较快看到结果（外部转发场景量不大）
    await syslog_receiver.flush_pending()
    return {"queued": queued}


# ---------------------------------------------------------------------------
# loghost 批量下发 / 回滚（高危：真实改动设备配置）
# ---------------------------------------------------------------------------

class LoghostTargetRequest(BaseModel):
    device_ids: list[int] = Field(..., min_length=1, max_length=loghost.MAX_DEVICES_PER_REQUEST)
    address: str | None = Field(None, max_length=64, description="日志主机地址（平台侧地址）；留空取 SYSLOG_ADVERTISE_ADDRESS")
    port: int | None = Field(None, ge=1, le=65535)
    level: str = Field("informational", max_length=16)
    save: bool = Field(True, description="是否保存到启动配置（save force）")


class LoghostApplyRequest(LoghostTargetRequest):
    confirm: bool = Field(False, description="必须显式置 true 才会真正下发（防误操作）")


def _resolve_address(address: str | None) -> str:
    addr = (address or settings.SYSLOG_ADVERTISE_ADDRESS or "").strip()
    if not addr:
        raise HTTPException(
            status_code=422,
            detail="未提供日志主机地址。请显式指定（设备需能访问该地址），"
                   "或在 .env 配置 SYSLOG_ADVERTISE_ADDRESS",
        )
    return addr


async def _load_devices(db: AsyncSession, device_ids: list[int]) -> list[Device]:
    rows = (await db.execute(
        select(Device).where(Device.id.in_(device_ids)).order_by(Device.id)
    )).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail="所选设备不存在")
    return list(rows)


@router.get("/loghost/candidates")
async def loghost_candidates(_user: dict = Depends(current_user)):
    """本机可被设备访问的候选地址（多网卡时由使用者确认，不自动决定）。"""
    return {
        "candidates": loghost.local_address_candidates(),
        "configured": settings.SYSLOG_ADVERTISE_ADDRESS or None,
        "udp_port": settings.SYSLOG_UDP_PORT,
    }


@router.post("/loghost/preview")
async def loghost_preview(
    body: LoghostTargetRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(current_user),
):
    """预览将要下发的命令（不连接设备、不做任何改动）。"""
    address = _resolve_address(body.address)
    devices = await _load_devices(db, body.device_ids)
    try:
        # 华为与 H3C 仅「保存配置」这条不同（华为没有 save force），
        # 预览必须给出所选设备**实际**会执行的命令，否则会误导操作人。
        plain_commands = loghost.build_apply_commands(address, body.port, body.level, body.save)
        plain_rollback = loghost.build_rollback_commands(address, body.save)
        has_huawei = any(loghost.is_huawei(d) for d in devices)
        hw_commands = hw_rollback = None
        if has_huawei:
            hw_commands = loghost.build_apply_commands(
                address, body.port, body.level, body.save, huawei=True)
            hw_rollback = loghost.build_rollback_commands(address, body.save, huawei=True)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    variants = [{
        "vendor": "H3C / 其他",
        "huawei": False,
        "commands": plain_commands,
        "rollback_commands": plain_rollback,
    }]
    if has_huawei:
        variants.append({
            "vendor": "华为 VRP",
            "huawei": True,
            "commands": hw_commands,
            "rollback_commands": hw_rollback,
        })
    return {
        "address": address,
        "port": body.port or 514,
        "level": body.level,
        "save": body.save,
        "commands": plain_commands,
        "rollback_commands": plain_rollback,
        "variants": variants,
        "devices": [
            {
                "id": d.id, "name": d.name, "ip": d.ip,
                "protocol": d.mgmt_protocol or "ssh", "port": d.mgmt_port or 22,
                "vendor": d.vendor, "huawei": loghost.is_huawei(d),
                "commands": hw_commands if loghost.is_huawei(d) else plain_commands,
                "rollback_commands": hw_rollback if loghost.is_huawei(d) else plain_rollback,
            }
            for d in devices
        ],
        "note": "设备需能访问该地址的 UDP 端口；下发前平台会先快照 info-center 配置，"
                "失败可一键回滚。保存到启动配置（save=True）后重启不丢失。",
    }


@router.post("/loghost/apply")
async def loghost_apply(
    body: LoghostApplyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(admin_only),
):
    """批量下发 loghost（管理员操作；须 confirm=true）。

    流程：取 info-center 快照 → 逐条下发并检查错误标记 → 回验 → 落库留痕。
    任一设备失败不影响其它设备，结果逐台返回。
    """
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="该操作会真实修改设备配置，请先调用 /loghost/preview 确认命令，"
                   "再以 confirm=true 重新提交",
        )
    address = _resolve_address(body.address)
    try:
        devices = await _load_devices(db, body.device_ids)
        results = await loghost.apply_loghost_to_devices(
            db, devices, address, body.port, body.level, body.save,
            operator=user.get("sub"),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    ok = sum(1 for r in results if r["ok"])
    pending = sum(1 for r in results if r.get("state") == "unverified")
    await record_audit(
        db, user, "device_logs", "loghost_apply",
        f"向 {ok}/{len(results)} 台设备下发日志主机 {address}"
        + (f"（{pending} 台待人工确认）" if pending else ""),
        ip=get_client_ip(request),
    )
    return {
        "ok_count": ok,
        "failed_count": len(results) - ok - pending,
        # 「命令已下发但回读不可信」既不是成功也不是失败，单独一档：
        # 混进 failed 会诱导操作人重复下发（对生产设备是二次真实变更）
        "unverified_count": pending,
        "results": results,
    }


@router.post("/loghost/rollback")
async def loghost_rollback(
    body: LoghostApplyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(admin_only),
):
    """批量回滚 loghost（管理员操作；须 confirm=true）。

    地址优先取该设备最近一次下发记录，其次取请求参数；
    若下发前 info-center 处于关闭状态，会一并还原。
    """
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="该操作会真实修改设备配置，请以 confirm=true 重新提交",
        )
    devices = await _load_devices(db, body.device_ids)
    records = await loghost.latest_records(db, [d.id for d in devices])
    try:
        results = await loghost.rollback_loghost_on_devices(
            db, devices, body.address, body.save, operator=user.get("sub"), records=records,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    ok = sum(1 for r in results if r["ok"])
    pending = sum(1 for r in results if r.get("state") == "unverified")
    await record_audit(
        db, user, "device_logs", "loghost_rollback",
        f"回滚 {ok}/{len(results)} 台设备的日志主机配置"
        + (f"（{pending} 台待人工确认）" if pending else ""),
        ip=get_client_ip(request),
    )
    return {
        "ok_count": ok,
        "failed_count": len(results) - ok - pending,
        "unverified_count": pending,
        "results": results,
    }


# 「设备已在往平台发日志」的判定时间窗（天）。
#   太短：日志稀少的设备（一天几条）会被漏判成"没配"；
#   太长：设备早已改过配置、但缓冲区里的历史日志还在陆续到达时，会被误判成"还在发"。
LOG_HOST_RECEIVING_DAYS = 7


@router.get("/loghost/status")
async def loghost_status(
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(current_user),
):
    """各设备当前的日志主机状态（下发记录 + 实际收到的日志，两个来源合并）。

    1. **下发记录**（``device_loghost_configs`` 每台设备最新一条）—— 平台对它做过什么；
    2. **实际收到的日志**（``device_logs`` 近 :data:`LOG_HOST_RECEIVING_DAYS` 天）——
       设备**确实**把日志发到了本平台。

    只看来源 1 会漏掉「有人直接在设备上配好、平台没有记录」的情况：页面显示"未配置"，
    操作人很可能据此重复下发，对生产设备就是二次真实变更。来源 2 恰好补上这个缺口 ——
    而且它是**唯一能证明"配置真的生效了"的证据**：命令下发成功 ≠ 日志真的发过来了。
    """
    rows = (await db.execute(
        select(DeviceLogHostConfig)
        .options(joinedload(DeviceLogHostConfig.device))
        .order_by(DeviceLogHostConfig.id.desc())
        .limit(2000)
    )).scalars().all()
    latest: dict[int, DeviceLogHostConfig] = {}
    for r in rows:
        key = r.device_id if r.device_id is not None else -r.id
        if key not in latest:
            latest[key] = r

    # 实际收到过日志的设备（有没有日志到达，是"配置真的生效"最硬的证据）
    since = datetime.now(timezone.utc) - timedelta(days=LOG_HOST_RECEIVING_DAYS)
    traffic_rows = (await db.execute(
        select(DeviceLog.device_id, func.max(DeviceLog.received_at), func.count(DeviceLog.id))
        .where(DeviceLog.device_id.is_not(None), DeviceLog.received_at >= since)
        .group_by(DeviceLog.device_id)
    )).all()
    traffic: dict[int, tuple[datetime | None, int]] = {
        did: (last_at, n) for did, last_at, n in traffic_rows if did is not None
    }

    # 只有日志、没有下发记录的设备（平台从没对它下过发，但它在发日志）
    extra_ids = [did for did in traffic if did not in latest]
    dev_map: dict[int, tuple[str, str]] = {}
    if extra_ids:
        for did, dname, dip in (await db.execute(
            select(Device.id, Device.name, Device.ip).where(Device.id.in_(extra_ids))
        )).all():
            dev_map[did] = (dname, dip)

    def _stamp(dt: datetime | None) -> str | None:
        return dt.isoformat() if dt else None

    items: list[dict] = []
    for key, r in latest.items():
        dev = r.device
        did = r.device_id
        last_at, n = traffic.get(did, (None, 0)) if did is not None else (None, 0)
        items.append({
            "device_id": did,
            # 设备已从平台删除时退回记录里的快照，保证"改的是哪台设备"仍可辨认
            "device_name": dev.name if dev else r.device_name,
            "device_ip": dev.ip if dev else r.device_ip,
            "address": r.loghost_address,
            "port": r.loghost_port,
            "status": r.status,
            "message": r.message,
            "source": "both" if last_at else "record",
            "receiving": bool(last_at),
            "last_log_at": _stamp(last_at),
            "log_count": n,
            "applied_at": _stamp(r.applied_at),
            "rolled_back_at": _stamp(r.rolled_back_at),
            "created_at": _stamp(r.created_at),
            "operator": r.operator,
        })

    for did, (last_at, n) in traffic.items():
        if did in latest:
            continue
        name, ip = dev_map.get(did, ("未知设备", ""))
        items.append({
            "device_id": did,
            "device_name": name,
            "device_ip": ip,
            "address": settings.SYSLOG_ADVERTISE_ADDRESS or None,
            "port": settings.SYSLOG_UDP_PORT,
            # 平台没下发过，但日志确实在进来 —— 说明设备侧是配好的（手工配或历史遗留）
            "status": "receiving",
            "message": f"平台未记录下发操作，但近 {LOG_HOST_RECEIVING_DAYS} 天已收到该设备日志",
            "source": "traffic",
            "receiving": True,
            "last_log_at": _stamp(last_at),
            "log_count": n,
            "applied_at": None,
            "rolled_back_at": None,
            "created_at": None,
            "operator": None,
        })

    rank = {"applied": 0, "receiving": 1, "unverified": 2, "failed": 3, "rolled_back": 4}
    items.sort(key=lambda it: (rank.get(it["status"], 9), (it["device_name"] or "").lower()))

    # 「已接入」的两个来源取并集，且**不重复计数**：
    #   ① 日志真的在进来（最硬的证据，不管下发记录写了什么）；
    #   ② 平台确认下发成功/待确认，但还没见到日志（设备可能只是这段时间没事件）。
    # 记成 failed 却在发日志的设备（如 HSW1）算在 ① 里 —— 它明明是通的，
    # 若按记录只报"失败"，界面会自相矛盾（卡片数字与明细对不上）。
    receiving_n = sum(1 for it in items if it["receiving"])
    silent_n = sum(1 for it in items
                   if not it["receiving"] and it["status"] in ("applied", "unverified"))
    return {
        "items": items,
        "summary": {
            "managed": receiving_n + silent_n,
            "receiving": receiving_n,
            "applied_silent": silent_n,
            "failed": sum(1 for it in items
                          if it["status"] == "failed" and not it["receiving"]),
            "rolled_back": sum(1 for it in items
                               if it["status"] == "rolled_back" and not it["receiving"]),
        },
        "receiving_days": LOG_HOST_RECEIVING_DAYS,
        "platform_address": settings.SYSLOG_ADVERTISE_ADDRESS or None,
        "udp_port": settings.SYSLOG_UDP_PORT,
    }
