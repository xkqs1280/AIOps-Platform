"""安全监控 API 路由 - 外部实时威胁态势（FireHOL 开放情报 + ipwho.is 地理定位）

原「攻击演示」接口（stats/trend/surge/top-sources/top-targets/correlate/threat-intel）
已按需求移除，安全监控只保留外部实时威胁数据。
"""
import json
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.external_threat import ExternalThreatSnapshot

router = APIRouter(prefix="/security", tags=["security"])

EXTERNAL_SOURCE_NOTE = (
    "数据来源：FireHOL 开放威胁情报（真实恶意 IP/CIDR 列表，每日更新）"
    "+ ipwho.is 地理定位；反映全球恶意攻击源实时态势，仅供参考"
)

COUNTRY_NAMES = {
    "CN": "中国", "US": "美国", "RU": "俄罗斯", "DE": "德国", "NL": "荷兰",
    "FR": "法国", "GB": "英国", "BR": "巴西", "IN": "印度", "JP": "日本",
    "KR": "韩国", "SG": "新加坡", "HK": "中国香港", "TW": "中国台湾",
    "MO": "中国澳门", "VN": "越南", "ID": "印度尼西亚", "TR": "土耳其",
    "PL": "波兰", "UA": "乌克兰", "CA": "加拿大", "AU": "澳大利亚",
    "IT": "意大利", "ES": "西班牙", "IR": "伊朗", "TH": "泰国", "MY": "马来西亚",
    "ZZ": "未知",
}


@router.get("/external/latest")
async def external_threat_latest(
    session: AsyncSession = Depends(get_session),
):
    """最新外部威胁快照：恶意源总量、攻击类型分布、来源国 TOP、中国来源数。"""
    result = await session.execute(
        select(ExternalThreatSnapshot)
        .order_by(ExternalThreatSnapshot.sampled_at.desc())
        .limit(1)
    )
    snap = result.scalars().first()
    if snap is None:
        return {
            "latest": None,
            "source_note": EXTERNAL_SOURCE_NOTE,
            "message": "尚无外部威胁数据（采集服务启动后约 1 分钟内生成首批数据）",
        }

    try:
        type_data = json.loads(snap.type_data or "{}")
    except Exception:
        type_data = {}
    try:
        country_data = json.loads(snap.country_data or "{}")
    except Exception:
        country_data = {}

    # 来源国 TOP10（转中文名）
    country_top = sorted(country_data.items(), key=lambda kv: kv[1], reverse=True)[:10]
    country_list = [
        {
            "code": code,
            "name": COUNTRY_NAMES.get(code, code),
            "count": count,
            "is_china": code == "CN",
        }
        for code, count in country_top
    ]

    return {
        "latest": {
            "id": snap.id,
            "total_entries": snap.total_entries,
            "type_data": type_data,
            "country_top": country_list,
            "china_entries": snap.china_entries,
            "sampled_ips": snap.sampled_ips,
            "source": snap.source,
            "sampled_at": snap.sampled_at.isoformat() if snap.sampled_at else None,
        },
        "source_note": EXTERNAL_SOURCE_NOTE,
    }


@router.get("/external/history")
async def external_threat_history(
    hours: int = Query(24, ge=1, le=720),
    session: AsyncSession = Depends(get_session),
):
    """外部威胁快照时间序列（趋势）。"""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = await session.execute(
        select(ExternalThreatSnapshot)
        .where(ExternalThreatSnapshot.sampled_at >= since)
        .order_by(ExternalThreatSnapshot.sampled_at)
    )
    rows = result.scalars().all()
    return [
        {
            "sampled_at": r.sampled_at.isoformat() if r.sampled_at else None,
            "total_entries": r.total_entries,
            "china_entries": r.china_entries,
            "sampled_ips": r.sampled_ips,
        }
        for r in rows
    ]
