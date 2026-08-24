"""外部威胁情报采集服务 - 抓取 FireHOL 开源威胁情报 feed，聚合中国视角的实时网络攻击态势。

数据源（均公开免费、无需密钥）：
- FireHOL blocklist-ipsets（开放威胁情报，真实恶意 IP/CIDR 列表，每日更新）：
  - firehol_level1      → 高危恶意源
  - firehol_abusers_1d  → 24 小时滥用源（扫描/暴力破解等）
  - firehol_proxies     → 匿名代理/跳板
- ipwho.is：免费 IP 地理定位（https，无需 key），为恶意源标注来源国。

说明：外部数据仅反映全球恶意攻击源实时态势（真实恶意 IP 集合），
来源国分布可直观展示攻击源主要来自哪些国家/地区，供安全参考。
"""
import asyncio
import json
import logging
import re
import subprocess
import urllib.request
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from app.models.external_threat import ExternalThreatSnapshot

logger = logging.getLogger(__name__)

# 采集间隔（秒）：30 分钟
COLLECT_INTERVAL = 1800
CURL_TIMEOUT = 25
# 地理标注抽样上限（ipwho.is 免费额度充裕，300 IP/次足够代表性）
MAX_SAMPLE_IPS = 300

FEEDS = [
    {
        "name": "firehol_level1",
        "label": "高危恶意源",
        "urls": [
            "https://cdn.jsdelivr.net/gh/firehol/blocklist-ipsets@master/firehol_level1.netset",
            "https://raw.githubusercontent.com/firehol/blocklist-ipsets/master/firehol_level1.netset",
        ],
    },
    {
        "name": "firehol_abusers_1d",
        "label": "24h 滥用源",
        "urls": [
            "https://cdn.jsdelivr.net/gh/firehol/blocklist-ipsets@master/firehol_abusers_1d.netset",
            "https://raw.githubusercontent.com/firehol/blocklist-ipsets/master/firehol_abusers_1d.netset",
        ],
    },
    {
        "name": "firehol_proxies",
        "label": "匿名代理/跳板",
        "urls": [
            "https://raw.githubusercontent.com/firehol/blocklist-ipsets/master/firehol_proxies.netset",
            "https://cdn.jsdelivr.net/gh/firehol/blocklist-ipsets@master/firehol_proxies.netset",
        ],
    },
]

IP_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})(?:/\d{1,2})?$")


async def _curl(url: str) -> str:
    """用系统 curl 抓取（服务器已确认 curl 可用且可访问外部网络）。

    注：Windows 的 SelectorEventLoop 不支持 asyncio.create_subprocess_exec，
    改用 to_thread + subprocess.run 绕开（与 discovery_service 一致）。
    """
    def _do():
        try:
            proc = subprocess.run(
                ["curl", "-s", "--max-time", str(CURL_TIMEOUT), url],
                capture_output=True, timeout=CURL_TIMEOUT + 10,
            )
            return proc.stdout.decode("utf-8", errors="replace")
        except Exception:
            return ""
    return await asyncio.to_thread(_do)


def _parse_entries(text: str) -> tuple[set, set]:
    """解析 feed 文本，返回 (去重条目集合, 可用于地理标注的纯 IP 集合)。"""
    entries = set()
    plain_ips = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = IP_RE.match(line)
        if m:
            entries.add(line)
            plain_ips.add(m.group(1))
    return entries, plain_ips


async def _lookup_one(ip: str) -> str | None:
    """查询单个 IP 的来源国（ipwho.is），返回 country_code；失败返回 None。"""
    def _do():
        try:
            url = f"https://ipwho.is/{ip}"
            req = urllib.request.Request(url, headers={"User-Agent": "AIOps-ThreatMonitor/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode(errors="replace"))
            if isinstance(data, dict) and data.get("success"):
                return data.get("country_code") or None
        except Exception:
            pass
        return None
    return await asyncio.to_thread(_do)


async def _geo_lookup(ips: set[str]) -> dict[str, int]:
    """并发（限 10 路）为抽样 IP 标注来源国，返回 {countryCode: count}。"""
    country_counts: dict[str, int] = {}
    ip_list = list(ips)[:MAX_SAMPLE_IPS]
    sem = asyncio.Semaphore(10)

    async def _q(ip: str):
        async with sem:
            return ip, await _lookup_one(ip)

    results = await asyncio.gather(*[_q(ip) for ip in ip_list])
    for _ip, cc in results:
        if cc:
            country_counts[cc] = country_counts.get(cc, 0) + 1
    return country_counts


async def collect_snapshot() -> ExternalThreatSnapshot | None:
    """抓取全部 feed → 聚合 → 地理标注 → 写入快照表。"""
    all_entries: set[str] = set()
    all_plain_ips: set[str] = set()
    type_data: dict[str, int] = {}

    for feed in FEEDS:
        text = None
        for url in feed["urls"]:
            try:
                candidate = await _curl(url)
                if candidate and len(candidate) > 100:
                    text = candidate
                    break
            except Exception as e:
                logger.warning(f"[{feed['name']}] 抓取失败 {url}: {e}")
        if not text:
            logger.warning(f"[{feed['name']}] 所有通道均失败，跳过")
            continue
        entries, ips = _parse_entries(text)
        type_data[feed["label"]] = len(entries)
        all_entries |= entries
        all_plain_ips |= ips
        logger.info(f"[{feed['name']}] {len(entries)} 条")

    if not all_entries:
        logger.error("所有外部 feed 均失败，本次不写入快照")
        return None

    country_counts = await _geo_lookup(all_plain_ips)
    sampled = sum(country_counts.values())
    china = country_counts.get("CN", 0)

    from app.database import async_session
    async with async_session() as db:
        snap = ExternalThreatSnapshot(
            total_entries=len(all_entries),
            type_data=json.dumps(type_data, ensure_ascii=False),
            country_data=json.dumps(country_counts, ensure_ascii=False),
            china_entries=china,
            sampled_ips=sampled,
        )
        db.add(snap)
        await db.commit()
        await db.refresh(snap)

    logger.info(
        f"外部威胁快照已入库: 恶意源条目 {len(all_entries)}, "
        f"抽样标注 {sampled} IP, 中国来源 {china}, 类型 {list(type_data.items())}"
    )
    return snap


async def threat_collect_loop():
    """后台采集循环 - 启动后立即采集一次，之后每 30 分钟一次。"""
    await asyncio.sleep(30)
    logger.info(f"External threat collector started (interval={COLLECT_INTERVAL}s)")
    while True:
        try:
            await collect_snapshot()
        except Exception as e:
            logger.error(f"External threat collect error: {type(e).__name__}: {e}")
        await asyncio.sleep(COLLECT_INTERVAL)


async def get_latest_snapshot(db) -> ExternalThreatSnapshot | None:
    result = await db.execute(
        select(ExternalThreatSnapshot).order_by(ExternalThreatSnapshot.sampled_at.desc()).limit(1)
    )
    return result.scalars().first()
