# -*- coding: utf-8 -*-
"""NTP 授时 API：状态 / 开关 / 上行偏差 / 手动同步 / 配置下发与回滚。

对应「NTP 授时功能 P1」。分四组：

  1. **状态与开关** —— 两个独立开关（下行授时 ``server`` / 上行取时 ``upstream``），
     默认都关闭；关闭即释放 UDP 123，不重启进程。
  2. **偏差查询** —— 当前偏差 + 历史曲线（供大屏与设置页）。
  3. **手动同步** —— 管理员专用，二次确认，且**只允许小幅校正**（见
     ``ntp_service.set_system_time``）。
  4. **配置下发 / 回滚** —— 高危：真实改动设备配置。必须先 preview 看命令，
     apply 时显式 ``confirm=true``；全程先快照再下发后回验，可随时回滚。

安全约定与其他配置类接口一致：下发/回滚/同步全部走 ``admin_only`` 并写审计日志 ——
"改设备配置"和"改平台时钟"都是必须留痕的运维动作。
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.device import Device
from app.models.ntp import (
    NtpCommandTemplate,
    NtpDeviceConfig,
    NtpOffsetRecord,
    NtpSetting,
)
from app.routers.auth import admin_only, current_user
from app.services import ntp_config_service as ntp_cfg
from app.services import ntp_service
from app.services.audit_service import get_client_ip, record_audit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ntp", tags=["NTP 授时"])

# 偏差曲线默认回看窗口
OFFSET_HISTORY_HOURS = 24
OFFSET_HISTORY_LIMIT = 2000


# ---------------------------------------------------------------------------
# 状态与开关
# ---------------------------------------------------------------------------

def _server_time_fields() -> dict:
    """宿主当前时间（供页面顶部「平台实时时钟」）。

    除了时间戳，还要给**宿主自己的时区偏移** —— 页面要显示的是"平台时间"，
    必须按宿主时区渲染，而不是按浏览器时区：远程运维时两者常常不同，
    按浏览器渲染会指着一个错的钟、却告诉操作人"平台时间准"。
    """
    import time as _time

    now = datetime.now().astimezone()
    off = now.utcoffset()
    return {
        "server_time": now.isoformat(),
        "server_time_epoch": _time.time(),
        # 用分钟数传给前端（如 +480 / -210），由前端渲染成 UTC+08:00 —— 传小时会把
        # 半小时时区（UTC+5:30）算歪。
        "server_timezone_offset_minutes": (
            int(round(off.total_seconds() / 60)) if off else 0
        ),
        "server_timezone_name": now.tzname() or "",
    }


@router.get("/status")
async def ntp_status(
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(current_user),
):
    """NTP 服务完整状态（两条链路 + 持久化设置 + 宿主当前时间）。"""
    row = await ntp_service.get_settings_row(db)
    status = ntp_service.server_status()
    status.update({
        "upstream_server_effective": ntp_service.effective_upstream_server(row),
        "offset_threshold_seconds": (
            row.offset_threshold_seconds if row is not None
            else settings.NTP_OFFSET_THRESHOLD_SECONDS
        ),
        "manual_sync_max_seconds": settings.NTP_MANUAL_SYNC_MAX_SECONDS,
    })
    status.update(_server_time_fields())
    return status


class NtpToggleRequest(BaseModel):
    server_enabled: bool | None = Field(None, description="下行授时（平台作 NTP 服务器）")
    upstream_enabled: bool | None = Field(None, description="上行取时（读上游偏差）")
    upstream_server: str | None = Field(None, max_length=128)
    listen_host: str | None = Field(None, max_length=64)
    offset_threshold_seconds: float | None = Field(None, gt=0, le=3600)


@router.put("/settings")
async def update_ntp_settings(
    body: NtpToggleRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(admin_only),
):
    """更新 NTP 设置与开关（管理员，持久化 + 审计）。

    - **关闭下行授时**：立即解绑 UDP 123（把端口让回给宿主自身的 NTP 服务），
      设备侧会开始同步不上，请确认不再需要平台授时；
    - **关闭上行取时**：停止主动向上游取时，已有偏差曲线/历史记录不受影响；
    - 设置持久化，重启平台后保持，不会"关了又自己开"。
    """
    if body.server_enabled is not None and not settings.NTP_SERVER_ENABLED:
        raise HTTPException(
            status_code=409,
            detail="下行授时已被部署配置禁用（.env 的 NTP_SERVER_ENABLED=false），"
                   "请在配置文件开启后重启平台",
        )
    if body.upstream_enabled is not None and not settings.NTP_UPSTREAM_ENABLED:
        raise HTTPException(
            status_code=409,
            detail="上行取时已被部署配置禁用（.env 的 NTP_UPSTREAM_ENABLED=false），"
                   "请在配置文件开启后重启平台",
        )

    row = await ntp_service.get_settings_row(db)
    if row is None:
        # 新建设置行。初值取「默认关闭」——下面再按请求逐项覆盖，
        # 不要在这里硬编码 True/False 当成最终值（曾因此丢掉用户的开启意图：
        # 行被建出来时是 False，而请求里的 server_enabled 只改了运行态没写进库，
        # 于是重启后开关又变回关闭）。
        row = NtpSetting(server_enabled=False, upstream_enabled=False)
        db.add(row)

    changes: list[str] = []
    # 开关本身也要落库（持久化）。运行态在下面统一切换 —— 先落库再切运行态：
    # 切运行态会立刻绑/解绑端口，若落库失败就会出现"运行态已改但重启后又变回去"
    # 的不一致，运维会以为开关没用。
    if body.server_enabled is not None:
        row.server_enabled = bool(body.server_enabled)
    if body.upstream_enabled is not None:
        row.upstream_enabled = bool(body.upstream_enabled)
    if body.upstream_server is not None:
        new_server = body.upstream_server.strip()
        if new_server != (row.upstream_server or ""):
            changes.append(f"上游时间源 {row.upstream_server or '(默认)'} → {new_server or '(默认)'}")
            row.upstream_server = new_server or None
    if body.listen_host is not None:
        new_host = body.listen_host.strip() or "0.0.0.0"
        if new_host != row.listen_host:
            changes.append(f"监听地址 {row.listen_host} → {new_host}")
            row.listen_host = new_host
    if body.offset_threshold_seconds is not None:
        if float(body.offset_threshold_seconds) != float(row.offset_threshold_seconds):
            changes.append(
                f"偏差告警阈值 {row.offset_threshold_seconds}s → "
                f"{body.offset_threshold_seconds}s"
            )
            row.offset_threshold_seconds = float(body.offset_threshold_seconds)
    row.operator = user.get("sub")

    # 先落库再切运行态：切运行态会立刻绑/解绑端口，若落库失败就会出现
    # "运行态已改但重启后又变回去"的不一致，运维会以为开关没用。
    await db.commit()

    if body.server_enabled is not None:
        result = ntp_service.set_server_enabled(body.server_enabled)
        changes.append(f"下行授时 {'开启' if result else '关闭'}")
    if body.upstream_enabled is not None:
        result = ntp_service.set_upstream_enabled(body.upstream_enabled)
        changes.append(f"上行取时 {'开启' if result else '关闭'}")

    if changes:
        await record_audit(
            db, user, "ntp", "settings_update", "；".join(changes),
            ip=get_client_ip(request),
        )

    status = ntp_service.server_status()
    status["upstream_server_effective"] = ntp_service.effective_upstream_server(row)
    status["offset_threshold_seconds"] = row.offset_threshold_seconds
    status["manual_sync_max_seconds"] = settings.NTP_MANUAL_SYNC_MAX_SECONDS
    status["changes"] = changes
    return status


# ---------------------------------------------------------------------------
# 偏差查询
# ---------------------------------------------------------------------------

@router.get("/offset")
async def current_offset(
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(current_user),
):
    """当前平台与上游的时钟偏差（读最近一条成功样本）。"""
    row = (await db.execute(
        select(NtpOffsetRecord)
        .where(NtpOffsetRecord.success.is_(True))
        .order_by(NtpOffsetRecord.sampled_at.desc(), NtpOffsetRecord.id.desc())
        .limit(1)
    )).scalar_one_or_none()

    setting = await ntp_service.get_settings_row(db)
    threshold = (setting.offset_threshold_seconds if setting is not None
                 else settings.NTP_OFFSET_THRESHOLD_SECONDS)

    if row is None:
        return {
            "available": False,
            "offset_seconds": None,
            "message": "尚无偏差样本。请在「时钟同步」页开启上行取时，或手动同步一次。",
            "threshold_seconds": threshold,
        }
    offset = float(row.offset_seconds)
    return {
        "available": True,
        "offset_seconds": offset,
        "abs_seconds": abs(offset),
        "round_trip_seconds": row.round_trip_seconds,
        "server": row.source_address,
        "source": row.source,
        "sampled_at": row.sampled_at,
        "threshold_seconds": threshold,
        # 超过阈值时前端标红；后端也给结论，避免两端各写一套判定
        "exceeded": abs(offset) > threshold,
        # 符号语义（RFC 5905）：offset = 上游时间 - 平台时间。
        # offset > 0 → 上游快、平台慢；offset < 0 → 上游慢、平台快。
        # 这里给前端一个语义化方向，避免两端各写一套判定、还容易把快慢说反。
        "direction": "slow" if offset > 0 else ("fast" if offset < 0 else "synced"),
    }


@router.get("/offset/history")
async def offset_history(
    hours: int = Query(OFFSET_HISTORY_HOURS, ge=1, le=720),
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(current_user),
):
    """偏差历史（供曲线图）：只返回成功样本，失败样本单独计数。"""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = (await db.execute(
        select(NtpOffsetRecord)
        .where(NtpOffsetRecord.sampled_at >= since)
        .order_by(NtpOffsetRecord.sampled_at.desc())
        .limit(OFFSET_HISTORY_LIMIT)
    )).scalars().all()

    points = [
        {
            "at": r.sampled_at,
            "offset_seconds": float(r.offset_seconds),
            "round_trip_seconds": r.round_trip_seconds,
            "source": r.source,
            "success": bool(r.success),
        }
        for r in reversed(rows)  # 按时间正序给图表
    ]
    ok = [p for p in points if p["success"]]
    setting = await ntp_service.get_settings_row(db)
    threshold = (setting.offset_threshold_seconds if setting is not None
                 else settings.NTP_OFFSET_THRESHOLD_SECONDS)
    values = [abs(p["offset_seconds"]) for p in ok]
    return {
        "points": points,
        "count": len(points),
        "success_count": len(ok),
        "failed_count": len(points) - len(ok),
        "threshold_seconds": threshold,
        "max_abs_seconds": max(values) if values else None,
        "min_abs_seconds": min(values) if values else None,
        "hours": hours,
    }


class ManualSyncRequest(BaseModel):
    confirm: bool = Field(False, description="必须显式置 true 才会真正校正宿主时钟")
    server: str | None = Field(None, max_length=128, description="临时指定上游源；留空用当前设置")


@router.post("/sync")
async def manual_sync(
    body: ManualSyncRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(admin_only),
):
    """管理员手动同步平台时钟（**高危**：会真实改写宿主时间）。

    设计约束（见 ``ntp_service.set_system_time``）：
      - 仅管理员；
      - 必须 ``confirm=true``；
      - 偏差超过 ``NTP_MANUAL_SYNC_MAX_SECONDS`` 一律拒绝 ——
        那么大的偏差是配置错误（上游地址/时区/宿主 NTP 服务），不是漂移，
        直接校正会掩盖真正的故障。
    """
    if not body.confirm:
        raise HTTPException(
            status_code=422,
            detail="未确认。手动同步会真实改写平台宿主时钟，"
                   "请确认后以 confirm=true 再次提交",
        )

    result = await ntp_service.manual_sync(body.server)

    await record_audit(
        db, user, "ntp", "manual_sync",
        (f"手动同步{'成功' if result['success'] else '失败'}：{result['message']}"
         f"（偏差 {result['offset_seconds']}s，源 {result['server']}）"),
        ip=get_client_ip(request),
    )
    if not result["success"]:
        # 用 422 而不是 500：这是"请求本身无法完成"（超限/无工具/无权限），
        # 而不是服务端故障；500 会让前端以为是平台 bug。
        raise HTTPException(status_code=422, detail=result["message"])
    return result


# ---------------------------------------------------------------------------
# 配置下发 / 回滚（高危）
# ---------------------------------------------------------------------------

class NtpTargetRequest(BaseModel):
    device_ids: list[int] = Field(..., min_length=1,
                                  max_length=ntp_cfg.MAX_DEVICES_PER_REQUEST)
    address: str | None = Field(None, max_length=64,
                                description="NTP 服务器地址（平台侧地址）；留空取 NTP_ADVERTISE_ADDRESS")
    clock_mode: str = Field("system", max_length=16,
                            description="system=全局时钟协议；mdc=多 MDC 场景")
    save: bool = Field(True, description="是否保存到启动配置（save force / save）")


class NtpCommandOverride(BaseModel):
    """本次下发携带的临时命令模板（preview / apply 共用）。

    做成"本次覆盖"而不是"必须先去保存"，是为了让人能临时试一条命令而不用把
    默认模板改掉；同时保证页面确认弹窗里显示的命令与实际下发**完全一致**。
    """

    h3c_commands: str | None = Field(
        None, max_length=ntp_cfg.MAX_TEMPLATE_BODY_LEN,
        description="本次下发的 H3C 命令模板（多行）；留空用已保存/默认模板",
    )
    huawei_commands: str | None = Field(
        None, max_length=ntp_cfg.MAX_TEMPLATE_BODY_LEN,
        description="本次下发的华为 VRP 命令模板（多行）；留空用已保存/默认模板",
    )


class NtpPreviewRequest(NtpTargetRequest, NtpCommandOverride):
    """预览请求（不连设备，纯本地渲染）。"""


class NtpApplyRequest(NtpPreviewRequest):
    confirm: bool = Field(False, description="必须显式置 true 才会真正下发（防误操作）")


def _collect_overrides(body) -> dict[str, str]:
    """收集本次下发的临时命令模板（**先校验，再连设备**）。

    模板非法属"请求本身有问题"，必须在连设备之前拦掉 —— 否则会变成
    "连了 10 台设备、每台都失败、失败原因都写着同一句模板错误"，既慢又难读。
    """
    out: dict[str, str] = {}
    for vendor, raw in ((ntp_cfg.VENDOR_H3C, getattr(body, "h3c_commands", None)),
                        (ntp_cfg.VENDOR_HUAWEI, getattr(body, "huawei_commands", None))):
        if raw is None or not raw.strip():
            continue
        try:
            ntp_cfg.validate_template_body(raw)
        except ValueError as e:
            label = "H3C" if vendor == ntp_cfg.VENDOR_H3C else "华为 VRP"
            raise HTTPException(
                status_code=422, detail=f"{label} 下发模板不合法：{e}",
            ) from e
        out[vendor] = raw
    return out


async def _load_devices(db: AsyncSession, device_ids: list[int]) -> list[Device]:
    rows = (await db.execute(
        select(Device).where(Device.id.in_(device_ids)).order_by(Device.id)
    )).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail="所选设备不存在")
    return list(rows)


def _resolve_address(address: str | None) -> str:
    try:
        return ntp_cfg.resolve_address(address)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/devices/candidates")
async def device_address_candidates(_user: dict = Depends(current_user)):
    """本机可被设备访问的候选地址（多网卡时由使用者确认，不自动决定）。"""
    return {
        "candidates": ntp_cfg.local_address_candidates(),
        "configured": settings.NTP_ADVERTISE_ADDRESS or None,
        "port": settings.NTP_PORT,
    }


@router.post("/devices/preview")
async def ntp_preview(
    body: NtpPreviewRequest,
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(current_user),
):
    """预览将要下发的命令（不连接设备、不做任何改动）。

    与 apply 走**同一套**模板解析（本次覆盖 > 库中保存 > 内置默认）：
    页面确认弹窗里显示的命令必须就是实际会发出去的那几条，否则这种"预览"反而害人。
    """
    address = _resolve_address(body.address)
    devices = await _load_devices(db, body.device_ids)
    try:
        mode = ntp_cfg.validate_clock_mode(body.clock_mode)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    overrides = _collect_overrides(body)
    custom = await ntp_cfg.load_command_templates(db)

    def _apply_cmds(vendor: str, huawei: bool) -> list[str]:
        lines = ntp_cfg.resolve_template_lines(
            custom, vendor, ntp_cfg.ACTION_APPLY, overrides.get(vendor),
        )
        return ntp_cfg.render_template(lines, address, mode, body.save, 1, huawei)

    def _rollback_cmds(vendor: str, huawei: bool) -> list[str]:
        lines = ntp_cfg.resolve_template_lines(custom, vendor, ntp_cfg.ACTION_ROLLBACK)
        return ntp_cfg.render_template(lines, address, mode, body.save, 1, huawei)

    # 华为与 H3C 的命令集不同（保存命令、时钟协议命令），
    # 预览必须给出所选设备**实际**会执行的命令，否则会误导操作人。
    plain_commands = _apply_cmds(ntp_cfg.VENDOR_H3C, False)
    plain_rollback = _rollback_cmds(ntp_cfg.VENDOR_H3C, False)
    has_huawei = any(ntp_cfg.is_huawei(d) for d in devices)
    hw_commands = hw_rollback = None
    if has_huawei:
        hw_commands = _apply_cmds(ntp_cfg.VENDOR_HUAWEI, True)
        hw_rollback = _rollback_cmds(ntp_cfg.VENDOR_HUAWEI, True)

    mdc_devices = [{"id": d.id, "name": d.name, "ip": d.ip}
                   for d in devices if ntp_cfg.is_mdc_device(d)]

    # 页面据此提示「本次用的是自定义模板」—— 自定义模板会让命令偏离内置定稿，
    # 操作人有权在点确认之前知道这件事。
    using_custom = {
        ntp_cfg.VENDOR_H3C: (
            overrides.get(ntp_cfg.VENDOR_H3C) is not None
            or (ntp_cfg.VENDOR_H3C, ntp_cfg.ACTION_APPLY) in custom
        ),
        ntp_cfg.VENDOR_HUAWEI: (
            overrides.get(ntp_cfg.VENDOR_HUAWEI) is not None
            or (ntp_cfg.VENDOR_HUAWEI, ntp_cfg.ACTION_APPLY) in custom
        ),
    }

    return {
        "address": address,
        "clock_mode": mode,
        "port": settings.NTP_PORT,
        "save": body.save,
        "commands": plain_commands,
        "rollback_commands": plain_rollback,
        "huawei_commands": hw_commands,
        "huawei_rollback_commands": hw_rollback,
        "device_count": len(devices),
        "has_huawei": has_huawei,
        "using_custom_apply": using_custom,
        # 疑似 MDC 设备主动提示：形态选错会下发失败，提前让人确认比事后排查便宜
        "mdc_devices": mdc_devices,
    }


@router.post("/devices/apply")
async def ntp_apply(
    body: NtpApplyRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(admin_only),
):
    """向所选设备下发 NTP 配置（管理员，高危，必须 confirm）。

    流程：逐台快照 → 下发 → 回读回验 → 三态判定 → 落库留痕。
    单台最多 100 台（``MAX_DEVICES_PER_REQUEST``），并发 4（便于观察与人工介入）。
    """
    if not body.confirm:
        raise HTTPException(
            status_code=422,
            detail="未确认。该操作会真实修改设备配置，请先 preview 查看命令，"
                   "确认后以 confirm=true 再次提交",
        )
    address = _resolve_address(body.address)
    devices = await _load_devices(db, body.device_ids)
    overrides = _collect_overrides(body)

    try:
        results = await ntp_cfg.apply_ntp_to_devices(
            db, devices, address, body.clock_mode, body.save, user.get("sub"),
            overrides=overrides,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    await record_audit(
        db, user, "ntp", "apply",
        f"向 {len(devices)} 台设备下发 NTP 服务器 {address}"
        f"（形态 {body.clock_mode}"
        f"{'，使用页面上的自定义命令模板' if overrides else ''}）；"
        f"确认生效 {sum(1 for r in results if r.get('state') == 'applied')} 台，"
        f"待确认 {sum(1 for r in results if r.get('state') == 'unverified')} 台，"
        f"失败 {sum(1 for r in results if r.get('state') == 'failed')} 台",
        ip=get_client_ip(request),
    )
    return {"results": results, "address": address, "clock_mode": body.clock_mode}


@router.post("/devices/rollback")
async def ntp_rollback(
    body: NtpTargetRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(admin_only),
):
    """回滚所选设备的 NTP 配置（管理员，高危）。

    地址优先取各设备**最近一条下发记录**里的地址（未显式传 address 时）；
    没有记录又没传地址的设备会被标为失败并给出原因，而不是静默跳过。
    """
    devices = await _load_devices(db, body.device_ids)
    records = await ntp_cfg.latest_records(db, body.device_ids)

    address = (body.address or "").strip() or None
    if address:
        address = _resolve_address(address)

    try:
        results = await ntp_cfg.rollback_ntp_on_devices(
            db, devices, address, body.save, user.get("sub"), records=records,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    await record_audit(
        db, user, "ntp", "rollback",
        f"回滚 {len(devices)} 台设备的 NTP 配置；"
        f"确认回滚 {sum(1 for r in results if r.get('state') == 'rolled_back')} 台，"
        f"待确认 {sum(1 for r in results if r.get('state') == 'unverified')} 台，"
        f"失败 {sum(1 for r in results if r.get('state') == 'failed')} 台",
        ip=get_client_ip(request),
    )
    return {"results": results}


@router.get("/devices/status")
async def ntp_device_status(
    device_ids: str | None = Query(None, description="逗号分隔的设备 ID；留空返回全部有记录的设备"),
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(current_user),
):
    """各设备 NTP 配置的当前状态（只读，供设备列表与详情页展示）。

    与 loghost 的状态视图同构：把「下发记录」与「实测数据」合并成一个视图。
    """
    wanted: list[int] = []
    if device_ids:
        for part in device_ids.split(","):
            part = part.strip()
            if part.isdigit():
                wanted.append(int(part))

    stmt = select(NtpDeviceConfig).order_by(NtpDeviceConfig.id.desc())
    if wanted:
        stmt = stmt.where(NtpDeviceConfig.device_id.in_(wanted))
    rows = (await db.execute(stmt.limit(5000))).scalars().all()

    latest: dict[int | None, NtpDeviceConfig] = {}
    for r in rows:
        if r.device_id not in latest:
            latest[r.device_id] = r
    # 最近一条**失败**记录单独留存：失败→重试→成功是最常见的路径，只留最新一条的话，
    # 失败原因当场被成功记录覆盖 —— 而"上次为什么失败"正是重试前最该看的信息。
    latest_failure = ntp_cfg.latest_failure_by_device(rows)

    items = [
        {
            "device_id": r.device_id,
            "device_name": r.device_name,
            "device_ip": r.device_ip,
            "server_address": r.server_address,
            "clock_mode": r.clock_mode,
            "status": r.status,
            "message": r.message,
            "applied_at": r.applied_at,
            "rolled_back_at": r.rolled_back_at,
            "operator": r.operator,
            # 失败详情（含判定依据与下一步建议）。``commands`` 一并给出：
            # 排查第一问就是"到底发了什么"，让人不必再翻审计。
            "last_failed": (
                {
                    "message": f.message,
                    "at": f.created_at,
                    "address": f.server_address,
                    "clock_mode": f.clock_mode,
                    "operator": f.operator,
                    "commands": f.commands,
                }
                if (f := latest_failure.get(r.device_id)) else None
            ),
        }
        for r in latest.values()
    ]
    return {
        "items": items,
        "port": settings.NTP_PORT,
        "server_enabled": ntp_service.server_enabled(),
        "bound": ntp_service.server_status()["bound"],
    }


# ---------------------------------------------------------------------------
# 命令模板（页面可编辑；写入为管理员动作）
# ---------------------------------------------------------------------------
#
# 为什么模板要能改：命令集随型号/版本漂移（H3C MDC 要带 mdc N、华为没有
# clock protocol、现场可能要加 source/priority），写死在代码里等于"每处差异都要发包"。
# 库里只存"被改过的"，没记录就是代码内置默认 —— 所以全新部署无需预置数据。

def _template_item(row: NtpCommandTemplate | None, vendor: str, action: str) -> dict:
    """一条模板的展示视图（含默认值，便于页面「恢复默认」与差异对比）。"""
    default_body = ntp_cfg.default_template_body(vendor, action)
    customized = row is not None
    return {
        "vendor": vendor,
        "action": action,
        "body": row.body if customized else default_body,
        "default_body": default_body,
        "customized": customized,
        "operator": row.operator if customized else None,
        "updated_at": row.updated_at if customized else None,
    }


@router.get("/command-templates")
async def list_command_templates(
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(current_user),
):
    """设备下发的命令模板（厂商 × 动作 = 4 条）。未自定义的返回内置默认。"""
    rows = (await db.execute(select(NtpCommandTemplate))).scalars().all()
    saved = {(r.vendor, r.action): r for r in rows}
    return {
        "items": [_template_item(saved.get(k), k[0], k[1])
                  for k in ntp_cfg.TEMPLATE_KINDS],
        # 占位符说明随接口下发，避免页面自己硬编码一套（改渲染规则时两边不同步）
        "placeholders": [
            {
                "token": ntp_cfg.PLACEHOLDER_ADDRESS,
                "required": True,
                "desc": "本次下发的 NTP 服务器地址（平台地址）；必填，回验也依赖它",
            },
            {
                "token": ntp_cfg.PLACEHOLDER_CLOCK_PROTOCOL,
                "required": False,
                "desc": "按「时钟协议」渲染为 clock protocol ntp 或 "
                        "clock protocol ntp mdc N（华为 VRP 恒为空，该行会消失）",
            },
            {
                "token": ntp_cfg.PLACEHOLDER_MDC_ID,
                "required": False,
                "desc": "MDC 编号",
            },
        ],
        "limits": {
            "max_lines": ntp_cfg.MAX_TEMPLATE_LINES,
            "max_line_len": ntp_cfg.MAX_TEMPLATE_LINE_LEN,
        },
        "save_note": "模板里的 save 类命令行会按「保存配置」勾选自动增删，"
                     "因此写不写 save force / save 都可以。",
    }


class NtpTemplateRequest(BaseModel):
    vendor: str = Field(..., max_length=16, description="h3c / huawei")
    action: str = Field(..., max_length=16, description="apply（下发）/ rollback（回滚）")
    body: str = Field(
        "", max_length=ntp_cfg.MAX_TEMPLATE_BODY_LEN,
        description="模板正文（多行）。留空 = 恢复默认（删除自定义记录）",
    )


@router.put("/command-templates")
async def save_command_template(
    body: NtpTemplateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(admin_only),
):
    """保存或恢复某一套命令模板（管理员，写审计）。

    - ``body`` 非空 → 校验后落库（覆盖同 厂商×动作 的旧值）；
    - ``body`` 为空 → **恢复默认**（删除自定义记录，回到代码内置模板）。

    校验拒绝：空模板、超长/超行、不可见控制字符、缺 ``{address}`` 占位符。
    最后一条是硬要求 —— 没有它，下发后回验（靠回读里找该地址）必然判失败或不可判定，
    与其让人对着一堆"待确认"排查，不如在保存时就拒绝。
    """
    vendor = (body.vendor or "").strip().lower()
    action = (body.action or "").strip().lower()
    if (vendor, action) not in ntp_cfg.DEFAULT_TEMPLATES:
        raise HTTPException(
            status_code=422,
            detail=f"不支持的模板 {body.vendor}/{body.action}"
                   "（仅 h3c|huawei × apply|rollback）",
        )

    row = (await db.execute(
        select(NtpCommandTemplate).where(
            NtpCommandTemplate.vendor == vendor,
            NtpCommandTemplate.action == action,
        )
    )).scalar_one_or_none()

    text = (body.body or "").strip()
    if not text:
        # 恢复默认：删记录而不是存一份与默认相同的正文 —— 否则以后改默认模板
        # （比如适配新型号）时，这些机器会一直卡在旧默认上，没人知道为什么。
        if row is not None:
            await db.delete(row)
            await db.commit()
        await record_audit(
            db, user, "ntp", "template_reset",
            f"恢复 {vendor}/{action} 命令模板为默认", ip=get_client_ip(request),
        )
        return {"item": _template_item(None, vendor, action), "reset": True}

    try:
        lines = ntp_cfg.validate_template_body(text)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    normalized = "\n".join(lines)

    if row is None:
        row = NtpCommandTemplate(
            vendor=vendor, action=action, body=normalized, operator=user.get("sub"),
        )
        db.add(row)
    else:
        row.body = normalized
        row.operator = user.get("sub")
    await db.commit()
    await db.refresh(row)

    await record_audit(
        db, user, "ntp", "template_update",
        f"更新 {vendor}/{action} 命令模板（{len(lines)} 行）",
        ip=get_client_ip(request),
    )
    return {"item": _template_item(row, vendor, action), "reset": False}
