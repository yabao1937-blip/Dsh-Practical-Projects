"""Shared source configuration and read-only decision calculations; no device actions."""
from typing import Literal
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session
from sqlalchemy import select, text

from ..database import get_db
from ..config import REMOTE_READONLY
from ..services.decision_live import calculate, LABELS
from ..services.state import load_store
from ..auth import require_write
from ..models import DecisionSourceRevision
from ..services.decision_sources import NODE_CELLS, GROUPS, config_at, config_view, shared_snapshot
from ..services.manual_measurements import POINT_METRICS
from ..telemetry.domain import iso_time, utc_now
from ..telemetry.models import TelemetryPoint
from ..telemetry.density_effective import DENSITY_ITEMS
import json
import re
from copy import deepcopy

router = APIRouter(prefix='/decision', tags=['实时决策计算'])


class InputCell(BaseModel):
    model_config = ConfigDict(extra='forbid')
    value: float | None = Field(None, strict=True, allow_inf_nan=False)
    status: Literal['good','manual','historical','default','held','stopped','missing'] = 'missing'
    unit: str = Field('',max_length=20)
    source: str = Field('',max_length=800)
    reason: str = Field('',max_length=2000)
    observed_at: str | None = Field(None,max_length=64)
    entered_at: str | None = Field(None,max_length=64)
    item_id: str = Field('',max_length=200)
    source_id: str = Field('',max_length=64)
    batch_id: str | None = Field(None,max_length=64)


class SnapshotInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    cells: dict[str,InputCell] = Field(max_length=len(LABELS))
    target_min: float = Field(8.5,ge=0,le=100,allow_inf_nan=False)
    target_max: float = Field(9.0,ge=0,le=100,allow_inf_nan=False)

    @model_validator(mode='after')
    def check(self):
        if self.target_min > self.target_max or set(self.cells)-LABELS.keys():
            raise ValueError('目标范围或输入字段不正确')
        return self


@router.post('/snapshots')
def snapshot(body: SnapshotInput, db: Session = Depends(get_db)):
    if db.bind.dialect.name == 'sqlite':
        db.execute(text('BEGIN'))
    return calculate({k:v.model_dump() for k,v in body.cells.items()},load_store(db),body.target_min,body.target_max)


class SourcesPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0, strict=True)
    choices: dict[str, str] = Field(default_factory=dict, max_length=len(NODE_CELLS))
    target_min: float | None = Field(None, ge=0, le=100, allow_inf_nan=False)
    target_max: float | None = Field(None, ge=0, le=100, allow_inf_nan=False)

    @model_validator(mode='after')
    def check_choices(self):
        for node, choice in self.choices.items():
            if node == 'coarse-amount' and choice:
                raise ValueError('粗精煤泥量固定按入洗原煤量 ÷ 24 计算，无需选择点位')
            if node not in NODE_CELLS or len(choice) > 160:
                raise ValueError('未知业务节点或来源过长')
            if choice == '':
                continue
            try:
                parts = json.loads(choice)
            except ValueError:
                raise ValueError('来源标识无效')
            if not isinstance(parts, list) or len(parts) != 2 or any(
                    not isinstance(p, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', p) for p in parts):
                raise ValueError('来源标识无效')
            self.choices[node] = json.dumps(parts, separators=(',', ':'))
        return self


@router.get('/sources')
def sources(db: Session = Depends(get_db)):
    if REMOTE_READONLY:
        from ..remote_readonly.startup import startup_view
        return startup_view('sources')
    return config_view(db)


@router.patch('/sources', dependencies=[Depends(require_write)])
def patch_sources(body: SourcesPatch, db: Session = Depends(get_db)):
    if db.bind.dialect.name == 'sqlite':
        db.execute(text('BEGIN IMMEDIATE'))
    current = config_at(db)
    if current['revision'] != body.revision:
        raise HTTPException(409, '目标范围或数据来源已被其他页面修改，请刷新后重试')
    config = {k:deepcopy(current[k]) for k in ('choices','target_min','target_max')}
    for node, choice in body.choices.items():
        if choice:
            source, key = json.loads(choice)
            group_key = {'system-switches':'four_system_status','desliming':'desliming_status','float-press':'float_press_status'}.get(node)
            point = db.get(TelemetryPoint, (source, key))
            if group_key and key != group_key:
                raise HTTPException(422, '此节点必须选择对应的设备状态组合')
            if not group_key and key in GROUPS:
                raise HTTPException(422, '数值指标不能选择设备状态组合')
            if node == 'density-current' and key != 'a303_a304_density_average' and (not point or point.config.get('item_id') not in DENSITY_ITEMS):
                raise HTTPException(422, '当前密度请选择 A303、A304 实际密度或两路平均')
            valid = key in POINT_METRICS if source == 'manual-measurements' else (
                (point is not None and point.enabled) or ((key in GROUPS or key == 'a303_a304_density_average')
                    and db.scalar(select(TelemetryPoint.key).where(TelemetryPoint.source_id == source,
                        TelemetryPoint.enabled.is_(True)).limit(1)) is not None))
            if not valid:
                raise HTTPException(422, '所选来源不存在，请刷新点位列表')
        config['choices'][node] = choice
    for key in ('target_min','target_max'):
        if getattr(body, key) is not None:
            config[key] = getattr(body, key)
    if config['target_min'] > config['target_max']:
        raise HTTPException(422, '目标灰分下限不能大于上限')
    if config == {k:current[k] for k in ('choices','target_min','target_max')} and current['revision']:
        return config_view(db)
    db.add(DecisionSourceRevision(effective_at=iso_time(utc_now()), config=config))
    db.commit()
    return config_view(db)


@router.get('/current')
def current_decision(db: Session = Depends(get_db)):
    from ..decision_hourly.density_policy_view import enabled,view
    if enabled():
        return view(db,'current')
    if REMOTE_READONLY:
        from ..remote_readonly.runtime import decision_view
        return decision_view('current')
    if db.bind.dialect.name == 'sqlite':
        db.execute(text('BEGIN'))
    return shared_snapshot(db, realtime=True)


@router.get('/hourly')
def hourly_decision(db: Session = Depends(get_db)):
    from ..decision_hourly.density_policy_view import enabled,view
    if enabled():
        return view(db,'hourly')
    if REMOTE_READONLY:
        from ..remote_readonly.runtime import decision_view
        return decision_view('hourly')
    from ..decision_hourly.service import read_hour
    if db.bind.dialect.name == 'sqlite':
        db.execute(text('BEGIN'))
    return read_hour(db,defer_missing=True)


@router.get('/experimental-density')
def experimental_density(db: Session = Depends(get_db)):
    from ..decision_hourly.density_candidates import current, require_enabled
    require_enabled()
    return current(db)


@router.get('/total-ash-hours')
def total_ash_hours(start: datetime | None = None, end: datetime | None = None, db: Session = Depends(get_db)):
    if REMOTE_READONLY and start is None and end is None:
        # Waiters share detached JSON and never check out a remote connection.
        from ..remote_readonly.runtime import recent_hours_view
        return recent_hours_view()
    from ..decision_hourly.ash_settlement import read_settled_hours
    if db.bind.dialect.name == 'sqlite':
        db.execute(text('BEGIN'))
    try:
        if REMOTE_READONLY:
            from ..remote_readonly.runtime import host_now
            return read_settled_hours(db, start, end, now=host_now(db))
        return read_settled_hours(db, start, end)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get('/total-ash-interval')
def total_ash_interval(start: datetime | None = None, end: datetime | None = None, db: Session = Depends(get_db)):
    from ..decision_hourly.ash_settlement import read_settled_interval
    if db.bind.dialect.name == 'sqlite':
        db.execute(text('BEGIN'))
    try:
        if REMOTE_READONLY:
            from ..remote_readonly.runtime import host_now
            return read_settled_interval(db, start, end, now=host_now(db))
        return read_settled_interval(db,start,end)
    except ValueError as error:
        raise HTTPException(422,str(error)) from error
