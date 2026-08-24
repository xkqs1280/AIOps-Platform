from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.alert import Alert, AlertRule
from app.models.device import Device
from app.schemas.alert import AlertRuleCreate, AlertRuleUpdate, AlertRuleResponse, AlertResponse, AlertListResponse, AlertStats

router = APIRouter(prefix="/alert-rules", tags=["告警规则"])


@router.get("", response_model=list[AlertRuleResponse])
async def list_rules(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AlertRule).order_by(AlertRule.id))
    return [AlertRuleResponse.model_validate(r) for r in result.scalars().all()]


@router.post("", response_model=AlertRuleResponse, status_code=201)
async def create_rule(data: AlertRuleCreate, db: AsyncSession = Depends(get_db)):
    rule = AlertRule(**data.model_dump())
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return AlertRuleResponse.model_validate(rule)


@router.put("/{rule_id}", response_model=AlertRuleResponse)
async def update_rule(rule_id: int, data: AlertRuleUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AlertRule).where(AlertRule.id == rule_id))
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="规则不存在")
    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(rule, key, value)
    await db.commit()
    await db.refresh(rule)
    return AlertRuleResponse.model_validate(rule)


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(rule_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(AlertRule).where(AlertRule.id == rule_id))
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(status_code=404, detail="规则不存在")
    await db.delete(rule)
    await db.commit()
