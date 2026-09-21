# -*- coding: utf-8 -*-
"""访问 IP 白名单：来源 IP 判定、名单匹配、进程内 TTL 缓存。

**改这个文件之前，先读这三条安全语义：**

1. **默认关闭** —— ``ip_whitelist_enabled=False`` 时不做任何限制，老部署升级后行为不变。
   绝不能让「新增的安全功能」把现场挡在平台门外。
2. **回环永久放行** —— 127.0.0.1 / ::1 永远允许。这是误锁后唯一的逃生通道：
   在服务器本机打开平台就能改回来。所以名单配错最多锁住别人，锁不死自己。
3. **只信「来自回环」的转发头** —— ``X-Forwarded-For`` / ``X-Real-IP`` 仅当请求来自
   本机回环（同机 nginx 反代场景）时才采信；远端直连一律以 TCP 对端地址为准。
   否则任何人加一行 ``X-Forwarded-For: 10.0.0.1`` 就能绕过整个白名单。
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import time

from fastapi import Request
from sqlalchemy import select

from app.database import async_session
from app.models.access_control import AccessControlSetting, IpWhitelistEntry

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 5.0

# 进程内缓存：单条 SELECT 也够快，但白名单要挂在**每个** HTTP 请求上（含静态资源），
# 所以用 TTL 缓存扛住读放大；写操作后立即 invalidate()，多 worker 下最多延迟 TTL 生效。
_STATE: dict = {"ts": 0.0, "enabled": False, "networks": (), "loaded": False}
_LOCK: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    """延迟创建锁（asyncio.Lock 需要在事件循环里绑定，模块导入期可能还没有循环）。"""
    global _LOCK
    if _LOCK is None:
        _LOCK = asyncio.Lock()
    return _LOCK


# ---------------------------------------------------------------- 来源 IP 判定


def is_loopback_ip(value: str) -> bool:
    """是否为本机回环地址（127.0.0.0/8、::1）。解析失败一律当 False。"""
    try:
        return ipaddress.ip_address((value or "").split("%")[0]).is_loopback
    except ValueError:
        return False


def resolve_client_ip(request: Request) -> str:
    """取用于白名单判定的来源 IP。

    只有请求来自本机回环时才采信转发头 —— 把回环位视为「可信入口」（同机反代），
    此时 ``X-Forwarded-For`` 里的客户端地址才是真实来源；远端直连时该头可被随意伪造，
    一律忽略，以 TCP 对端地址为准。
    """
    host = request.client.host if request.client else ""
    if is_loopback_ip(host):
        forwarded = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip") or ""
        candidate = forwarded.split(",")[0].strip()
        if candidate:
            return candidate
    return host


# ---------------------------------------------------------------- 名单匹配


def normalize_cidr(value: str) -> str:
    """校验并规范化一条白名单（单个 IP 或 CIDR）。非法输入抛 ValueError。"""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("IP 或网段不能为空")
    try:
        if "/" in raw:
            network = ipaddress.ip_network(raw, strict=False)
            # 用户习惯写 192.168.1.5/24，按网段规范化成 192.168.1.0/24
            return str(network)
        address = ipaddress.ip_address(raw)
        return f"{address}/{address.max_prefixlen}"
    except ValueError as exc:
        raise ValueError(
            f"「{raw}」不是合法的 IP 或网段（单个 IP 如 192.168.1.10，网段如 192.168.1.0/24）"
        ) from exc


def normalize_entries(raw_entries: list[dict]) -> list[tuple[str, str]]:
    """把前端提交的条目规范化并去重，返回 [(cidr, remark)]。非法输入抛 ValueError。"""
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw_entries:
        cidr = normalize_cidr(str(item.get("cidr", "")))
        if cidr in seen:
            continue  # 同一网段写两遍没有意义，静默去重
        seen.add(cidr)
        result.append((cidr, str(item.get("remark") or "").strip()[:64]))
    return result


def compile_networks(cidrs) -> tuple:
    """把 cidr 文本编译成 ip_network 对象元组。非法条目跳过并告警。"""
    networks = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            logger.warning("跳过非法的白名单条目：%r", cidr)
    return tuple(networks)


def ip_in_networks(ip: str, networks) -> bool:
    """来源 IP 是否命中任一网段。IP 解析失败按「不命中」处理（fail-closed）。"""
    try:
        address = ipaddress.ip_address((ip or "").split("%")[0])
    except ValueError:
        return False
    return any(address in network for network in networks)


# ---------------------------------------------------------------- 状态读取（带缓存）


async def _read_state() -> dict:
    """从库里读开关与名单。

    读取失败时**沿用上次生效的配置**（只在日志留痕）：
    - 已启用白名单的部署遇到数据库抖动，策略要继续生效 —— 不能一读不到就放开，
      那等于给「把数据库打挂」变成了绕过白名单的手段；
    - 反过来，冷启动就读不到（``_STATE`` 初始为「未启用」）时不会拦截任何人，
      避免把「平台起不来」放大成「所有人都进不去」。

    同时刷新 ``ts``：数据库长时间不可用时，每个请求都去试一次连接只会全部卡在超时上。
    """
    try:
        async with async_session() as db:
            row = (
                await db.execute(
                    select(AccessControlSetting).order_by(AccessControlSetting.id).limit(1)
                )
            ).scalars().first()
            rows = (
                await db.execute(select(IpWhitelistEntry).order_by(IpWhitelistEntry.id))
            ).scalars().all()
    except Exception as exc:  # noqa: BLE001 - 存储异常不能让白名单把平台打死
        logger.warning("IP 白名单读取失败，沿用上次生效的配置：%s", exc)
        stale = dict(_STATE)
        stale["ts"] = time.monotonic()
        stale["loaded"] = True  # 让 TTL 生效，别让每个请求都去等一次连接超时
        return stale

    networks = compile_networks([row_entry.cidr for row_entry in rows])
    if len(networks) != len(rows):
        # 一条脏数据不该让整个平台拒绝服务，上面已逐条告警
        logger.warning("IP 白名单存在非法条目：库中 %s 条，有效 %s 条", len(rows), len(networks))
    return {
        "ts": time.monotonic(),
        "enabled": bool(row.ip_whitelist_enabled) if row else False,
        "networks": networks,
        "loaded": True,
    }


async def whitelist_state(*, force: bool = False) -> tuple[bool, tuple]:
    """返回 ``(是否启用, 网段元组)``。命中缓存时不查库（默认 TTL 5 秒）。"""
    fresh = _STATE["loaded"] and time.monotonic() - _STATE["ts"] < CACHE_TTL_SECONDS
    if fresh and not force:
        return _STATE["enabled"], _STATE["networks"]
    async with _lock():
        fresh = _STATE["loaded"] and time.monotonic() - _STATE["ts"] < CACHE_TTL_SECONDS
        if fresh and not force:
            return _STATE["enabled"], _STATE["networks"]
        _STATE.update(await _read_state())
    return _STATE["enabled"], _STATE["networks"]


def invalidate() -> None:
    """配置变更后立即失效缓存，新名单对后续请求即刻生效。"""
    _STATE["ts"] = 0.0


def is_allowed(client_ip: str, networks) -> bool:
    """放行判定：回环永远放行，其余看名单。"""
    return is_loopback_ip(client_ip) or ip_in_networks(client_ip, networks)
