"""GPT evidence and append-only experiment notes, independent of DS/current manual inputs."""
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text, func
from sqlalchemy.orm import Session

from ..auth import require_write
from ..database import get_db
from ..hourly_brief.inputs import business_time,LOCAL
from ..routers.manual_measurements import MeasurementRoute
from ..models import ManualMeasurement
from ..telemetry.domain import iso_time, utc_now
from .models import DecisionCycleHour, DecisionCycleSample, DensityAction, DensityActionEntry
from .tracking import action_view,assess_window
from .archive import read_sample, archived_rows
from .service import window
from .evidence import summarize
from .references import hydrate_rows

router = APIRouter(prefix='/decision/evidence',tags=['GPT 工况与调密跟踪'],route_class=MeasurementRoute)


def validate_hour(start):
    if not 1970<=start.year<=9998 or start.tzinfo is None:
        raise HTTPException(422,'请选择 1970–9998 年含时区的整点小时')
    local=start.astimezone(LOCAL)
    if local.minute or local.second or local.microsecond:
        raise HTTPException(422,'请选择北京时间整点小时')
    if start>utc_now():
        raise HTTPException(422,'不能读取未来小时')


@router.get('/hours')
def hours(limit: int=Query(24,ge=1,le=168),offset: int=Query(0,ge=0,le=100000),db: Session=Depends(get_db)):
    now=utc_now()
    items = db.scalars(select(DecisionCycleHour).order_by(DecisionCycleHour.start.desc()).offset(offset).limit(limit))
    return dict(current_start=iso_time(window(now)[1]),checked_at=iso_time(now),items=[dict(start=r.start,end=r.end,policy=r.snapshot.get('policy'),
        segment_count=len((r.snapshot.get('evidence') or {}).get('segments',[]))) for r in items])


@router.get('/segments')
def segments(start: datetime | None=None,db: Session=Depends(get_db)):
    now=utc_now()
    if start is not None:
        validate_hour(start)
        row=db.get(DecisionCycleHour,iso_time(start))
        if not row:
            raise HTTPException(404,'该小时尚无保存记录')
        return dict(period=dict(start=row.start,end=row.end),frozen=True,
            evidence=row.snapshot.get('evidence'),reason='旧口径小时未保存分段' if not row.snapshot.get('evidence') else '')
    _,begin=window(now)
    rows=list(db.scalars(select(DecisionCycleSample).where(DecisionCycleSample.sampled_at>=iso_time(begin),
        DecisionCycleSample.sampled_at<=iso_time(now)).order_by(DecisionCycleSample.slot)))
    return dict(period=dict(start=iso_time(begin),end=iso_time(now)),frozen=False,evidence=summarize(hydrate_rows(db,rows),now))


@router.get('/samples/{slot}')
def sample(slot:int,db:Session=Depends(get_db)):
    if not 0 <= slot <= 50000000000:
        raise HTTPException(422,'采样标识超出范围')
    found=read_sample(db,slot)
    if not found:
        raise HTTPException(404,'该逐次记录不存在或启用前已清理')
    from .models import DecisionCycleContext
    context=db.get(DecisionCycleContext,found['context_key'])
    found['context_definition']=context.definition if context else None
    return found


@router.get('/samples')
def samples(start:datetime,limit:int=Query(40,ge=1,le=100),offset:int=Query(0,ge=0,le=720),db:Session=Depends(get_db)):
    validate_hour(start)
    end=start+timedelta(hours=1)
    rows=list(db.scalars(select(DecisionCycleSample).where(DecisionCycleSample.sampled_at>=iso_time(start),
        DecisionCycleSample.sampled_at<iso_time(end)).order_by(DecisionCycleSample.slot.desc())))
    data=[dict(slot=r.slot,sampled_at=r.sampled_at,context_key=r.context_key,snapshot=r.snapshot) for r in hydrate_rows(db,rows)]
    if not data:
        data=sorted(archived_rows(db,iso_time(start)),key=lambda r:r['slot'],reverse=True)
    return dict(total=len(data),items=[dict(slot=r['slot'],sampled_at=r['sampled_at'],context_key=r['context_key'],
        statuses={k:r['snapshot'].get('cells',{}).get(k,{}).get('status') for k in ('density','total_ash','suggested_density')},
        values={k:r['snapshot'].get('cells',{}).get(k,{}).get('value') for k in
                ('density','coarse_ash','heavy_ash','total_ash','suggested_density')}) for r in data[offset:offset+limit]])


@router.get('/actions')
def actions(limit:int=Query(20,ge=1,le=100),offset:int=Query(0,ge=0,le=100000),db:Session=Depends(get_db)):
    rows=db.scalars(select(DensityAction).order_by(DensityAction.detected_at.desc()).offset(offset).limit(limit))
    return dict(total=db.scalar(select(func.count()).select_from(DensityAction)),items=[
        dict(id=r.id,detected_at=r.detected_at,channel=r.channel,
             before=r.evidence['before']['pair']['setpoint'].get('value'),
             after=r.evidence['after']['pair']['setpoint'].get('value')) for r in rows])


def find_action(db,identifier):
    action=db.get(DensityAction,identifier)
    if not action:
        raise HTTPException(404,'调密记录不存在')
    return action


@router.get('/actions/{identifier}')
def detail(identifier:str,db:Session=Depends(get_db)):
    return action_view(db,find_action(db,identifier))


class Entry(BaseModel):
    model_config=ConfigDict(extra='forbid')
    client_id:UUID
    revision:int=Field(ge=0,strict=True)
    kind:Literal['confirmation','assay']
    operator:str=Field('',max_length=80)
    note:str=Field('',max_length=2000)
    origin:Literal['manual','automatic','unknown']='unknown'
    adoption:Literal['adopted','partial','not_adopted','unknown']='unknown'
    occurred_at:datetime | None=None
    phase:Literal['before','after'] | None=None
    value:float | None=Field(None,ge=0,le=100,allow_inf_nan=False,strict=True)
    manual_measurement_id:int | None=Field(None,gt=0)
    sample_start:datetime | None=None
    sample_end:datetime | None=None
    reported_at:datetime | None=None
    sample_location:str=Field('',max_length=200)
    sample_code:str=Field('',max_length=100)
    sample_type:Literal['grab','composite']='grab'

    @model_validator(mode='after')
    def check(self):
        for stamp in (self.occurred_at,self.sample_start,self.sample_end,self.reported_at):
            if stamp is not None and not 1970<=stamp.year<=9998:
                raise ValueError('时间应在 1970–9998 年范围内')
        if self.occurred_at is not None and self.occurred_at.tzinfo is None:
            raise ValueError('操作时间必须包含时区')
        if self.kind=='assay':
            if not all((self.phase,self.sample_start,self.sample_end,self.reported_at,self.sample_location.strip(),self.sample_code.strip())):
                raise ValueError('请填写样品编号、调前/调后、采样起止、出报告时间和采样位置')
            if self.value is None and self.manual_measurement_id is None:
                raise ValueError('请填写化验值或关联已有补录')
            stamps=(self.sample_start,self.sample_end,self.reported_at)
            if any(t.tzinfo is None for t in stamps):
                raise ValueError('采样与报告时间必须包含时区')
            if not self.sample_start <= self.sample_end <= self.reported_at:
                raise ValueError('时间顺序应为采样开始 ≤ 采样结束 ≤ 出报告')
            if self.reported_at > utc_now()+timedelta(seconds=5):
                raise ValueError('报告时间不能在未来')
            if self.sample_type=='grab' and self.sample_start!=self.sample_end:
                raise ValueError('瞬时样的起止时间应一致；区间样请选择混合样')
        return self


@router.post('/actions/{identifier}/entries',dependencies=[Depends(require_write)])
def append(identifier:str,body:Entry,db:Session=Depends(get_db)):
    if db.bind.dialect.name=='sqlite':
        db.execute(text('BEGIN IMMEDIATE'))
    action=find_action(db,identifier)
    content=body.model_dump(mode='json',exclude={'client_id','revision'})
    existing=db.scalar(select(DensityActionEntry).where(DensityActionEntry.client_id==str(body.client_id)))
    if existing:
        if existing.action_id!=identifier or existing.content.get('request')!=content:
            raise HTTPException(409,'提交标识已用于不同内容')
        return action_view(db,action)
    count=db.scalar(select(func.count()).select_from(DensityActionEntry).where(DensityActionEntry.action_id==identifier))
    if count!=body.revision:
        raise HTTPException(409,'跟踪记录已更新，请重新读取后提交')
    saved=dict(content)
    if body.occurred_at is not None and not business_time(action.evidence['before']['sampled_at']) <= body.occurred_at <= business_time(action.evidence['after']['sampled_at']):
        raise HTTPException(422,'操作时间应在两次有效设定值观测之间')
    if body.kind=='assay':
        before,after=action.evidence['before'],action.evidence['after']
        if body.phase=='before' and body.sample_end>business_time(before['sampled_at']):
            raise HTTPException(422,'调前样品必须在变化前最后一次观测之前结束')
        if body.phase=='after' and body.sample_start<business_time(after['sampled_at']):
            raise HTTPException(422,'调后样品必须在首次观测到变化之后开始')
        if body.manual_measurement_id:
            record=db.get(ManualMeasurement,body.manual_measurement_id)
            if not record or record.metric!='heavy_clean_coal_ash':
                raise HTTPException(422,'只能关联重介精煤灰分补录记录')
            if body.value is not None and abs(body.value-record.value)>1e-9:
                raise HTTPException(422,'填写值与关联补录值不一致')
            if not body.sample_start<=business_time(record.observed_at)<=body.sample_end:
                raise HTTPException(422,'关联补录的采样时间不在所填区间')
            saved['value']=record.value
            saved['linked_measurement']=dict(id=record.id,value=record.value,observed_at=record.observed_at,created_at=record.created_at)
        saved['operating_review']=assess_window(db,action,body.sample_start,body.sample_end)
        prior_samples=db.scalars(select(DensityActionEntry).where(DensityActionEntry.content['kind'].as_string()=='assay',
            DensityActionEntry.content['sample_code'].as_string()==body.sample_code))
        for prior in prior_samples:
            c=prior.content
            if business_time(c['sample_start'])==body.sample_start and c['sample_location']==body.sample_location:
                if any(c.get(k)!=saved.get(k) for k in ('value','sample_type','manual_measurement_id')) or business_time(c['sample_end'])!=body.sample_end:
                    raise HTTPException(409,'相同编号、时间和位置的样品内容不一致，请核对原始化验')
                if prior.action_id==identifier and c['phase']==body.phase:
                    raise HTTPException(409,'这份样品已关联到该次调密，请查看已有记录')
    saved['request']=content
    db.add(DensityActionEntry(action_id=identifier,client_id=str(body.client_id),created_at=iso_time(utc_now()),content=saved))
    db.commit()
    return action_view(db,action)
