"""Forward-only decision archive and immutable previous-hour summaries."""
from copy import deepcopy
from datetime import timedelta

from sqlalchemy import select, text, delete, exists

from ..database import SessionLocal
from ..hourly_brief.inputs import LOCAL, business_time
from ..services.decision_sources import shared_snapshot
from ..services.decision_live import gpt_group
from ..services.state import load_store
from ..telemetry.domain import iso_time, utc_now
from .models import DecisionCycleContext, DecisionCycleSample, DecisionCycleHour
from .statistics import CADENCE, digest, hour_advice
from .history import historical_cells


def window(now):
    end = now.replace(minute=0, second=0, microsecond=0)
    return end-timedelta(hours=1), end


def compact_cell(c):
    result = {k:deepcopy(c[k]) for k in ('value','status','reason','source','unit','observed_at','created_at',
        'record_id','metric','input_path','item_id','source_id','point_key','raw_value','density_label') if k in c}
    sample = c.get('sample') or {}
    if sample:
        result['batch_id'] = sample.get('batch_id')
        result['definition_id'] = sample.get('definition_id')
    if isinstance(c.get('components'),dict):
        result['components'] = {k:compact_cell(v) for k,v in c['components'].items()}
    return result


def context_definition(result, store):
    group = gpt_group(store) or {}
    method = result['model'].get('method')
    model = group.get(method) or {}
    # Save coefficients/policies once per context, independent of mutable current model rows.
    definitions = {}
    def collect(c):
        sample = c.get('sample') or {}
        if sample.get('definition_id'):
            identity = str([c.get('source_id'),c.get('point_key'),c.get('item_id')])
            definitions[identity] = dict(definition_id=sample['definition_id'],unit=c.get('unit'))
        parts = c.get('components') or []
        for part in parts.values() if isinstance(parts,dict) else parts:
            collect(part)
    for c in result['cells'].values():
        collect(c)
    return dict(policy='decision-hourly-v1', sources=result['sources']['resolved_choices'],point_definitions=definitions,
        target=result['target'], model=result['model'], model_definition=deepcopy(model) if method == 'expert9' else
        {k:deepcopy(model[k]) for k in ('intercept','coefs','feature_names','featureNames','inputPolicy','inputBounds','imputeMeans') if k in model},
        density_guide=store.get('densityGuide') or {})


def record(db, now, result=None, store=None):
    slot = int(now.timestamp())//CADENCE
    if db.get(DecisionCycleSample,slot):
        return False
    result = result or shared_snapshot(db)
    # A lock wait/computation crossing an hour cannot assign new inputs to the old hour.
    now = business_time(result['generated_at'])
    slot = int(now.timestamp())//CADENCE
    if db.get(DecisionCycleSample,slot):
        return False
    store = store if store is not None else load_store(db)
    context = context_definition(result,store)
    key = digest(context)
    if not db.get(DecisionCycleContext,key):
        db.add(DecisionCycleContext(key=key,definition=context))
        db.flush()
    cells = {k:compact_cell(c) for k,c in result['cells'].items()}
    identities = {k:{f:c.get(f) for f in ('value','status','observed_at','entered_at','source_id','item_id','batch_id')}
                  for k,c in result['inputs'].items()}
    snapshot = {k:deepcopy(result[k]) for k in ('sources','density_label','model','target',
        'coarse_ash','heavy_amount','total_ash','suggested_density','guidance','warnings')}
    snapshot['cells'] = cells
    snapshot['protection'] = {k:deepcopy(store.get(k)) for k in ('densityLastMoveAt','densityActionLatch')}
    db.add(DecisionCycleSample(slot=slot,sampled_at=iso_time(now),context_key=key,
        fingerprint=digest([key,identities]),snapshot=snapshot))
    return True


def build_hour(db, start, end, now):
    rows = list(db.scalars(select(DecisionCycleSample).where(DecisionCycleSample.sampled_at>=iso_time(start),
        DecisionCycleSample.sampled_at<iso_time(end)).order_by(DecisionCycleSample.slot)))
    cells, sources, basis = historical_cells(db,start,end,rows)
    context = db.get(DecisionCycleContext,rows[-1].context_key) if rows else None
    step = (context.definition.get('density_guide') or {}).get('maxStep',.03) if context else .03
    advice, coverage, guide = hour_advice(rows,start,end,basis,step)
    cells['suggested_density'] = advice
    for key in ('coarse_ash','total_ash'):
        if cells[key]['value'] is not None:
            cells[key]['reason'] += '；先逐次计算，再对结果求平均'
    return dict(policy='decision-hourly-v1', generated_at=iso_time(now),
        period=dict(start=iso_time(start),end=iso_time(end),apply_start=iso_time(end),apply_end=iso_time(end+timedelta(hours=1)),
            label=f'{start.astimezone(LOCAL):%m-%d %H:%M}–{end.astimezone(LOCAL):%H:%M}',
            apply_label=f'{end.astimezone(LOCAL):%H:%M}–{(end+timedelta(hours=1)).astimezone(LOCAL):%H:%M}'),
        cells=cells, sources=sources, density_label=sources['density_label'], target=dict(min=sources['target_min'],max=sources['target_max']),
        model=rows[-1].snapshot['model'] if rows else {}, context_key=rows[-1].context_key if rows else None,
        coverage=coverage,guidance=guide,density_basis=basis,
        coarse_ash=cells['coarse_ash'],heavy_amount=cells['heavy_amount'],total_ash=cells['total_ash'],suggested_density=advice,
        notes=['有效样本算术平均；停机零值、密度 1、缺失和沿用的采集值不重复计入。',
               '手工数据按实际记录条数平均；没有新增时沿用该小时前最近值并显示采样时间。',
               '旧采集历史只代表已保存的样本；功能启用前没有逐次预测和调密记录。'])


def tick(now=None, sessions=SessionLocal):
    with sessions() as db:
        # Serialize duplicate worker ticks; one slot/one hour even during overlapping restarts.
        if db.bind.dialect.name == 'sqlite':
            db.execute(text('BEGIN IMMEDIATE'))
        now = now or utc_now()
        start,end = window(now)
        record(db,now)
        if not db.get(DecisionCycleHour,iso_time(start)):
            db.add(DecisionCycleHour(start=iso_time(start),end=iso_time(end),generated_at=iso_time(now),
                                    snapshot=build_hour(db,start,end,now)))
        # Keep detailed observations for seven days; permanent frozen hour summaries remain.
        old_slots = list(db.scalars(select(DecisionCycleSample.slot).where(
            DecisionCycleSample.sampled_at < iso_time(start-timedelta(days=7)),
            exists(select(DecisionCycleHour.start).where(DecisionCycleHour.start<=DecisionCycleSample.sampled_at,
                                                       DecisionCycleHour.end>DecisionCycleSample.sampled_at))).limit(2000)))
        if old_slots:
            db.execute(delete(DecisionCycleSample).where(DecisionCycleSample.slot.in_(old_slots)))
        db.commit()


def read_hour(db, now=None, live=None):
    now = now or utc_now()
    start,end = window(now)
    saved = db.get(DecisionCycleHour,iso_time(start))
    result = deepcopy(saved.snapshot) if saved else build_hour(db,start,end,now)
    result['frozen'] = bool(saved)
    result['checked_at'] = iso_time(now)
    # A frozen historical offset must not bypass a current stop, disconnect, new source or protection hold.
    if result['suggested_density']['value'] is not None:
        current = live if live is not None else shared_snapshot(db)
        definition = context_definition(current,load_store(db))
        reason = None
        if digest(definition) != result['context_key']:
            reason = '当前来源、模型或调密参数已变化，等待新口径完整小时'
        elif current['suggested_density']['value'] is None or (current.get('guidance') or {}).get('hold'):
            reason = '当前状态暂缓调密：'+current['suggested_density'].get('reason','输入或保护条件不满足')
        if reason:
            result['suggested_density'] = dict(result['suggested_density'],value=None,status='missing',reason=reason)
            result['cells']['suggested_density'] = result['suggested_density']
            result['guidance'].update(rhoNew=None,reason=reason)
    return result
