"""Forward-only decision archive and immutable previous-hour summaries."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select, text

from ..database import SessionLocal
from ..hourly_brief.inputs import LOCAL, business_time
from ..services.decision_sources import shared_snapshot
from ..services.decision_live import gpt_group
from ..services.state import load_store
from ..telemetry.domain import iso_time, utc_now
from .models import DecisionCycleContext, DecisionCycleSample, DecisionCycleHour
from .statistics import CADENCE, digest, hour_advice, refresh_coverage_policy
from .history import historical_cells
from .evidence import POLICY, PROVISIONAL, SEGMENT_POLICY, summarize
from .tracking import density_channels, observe
from .archive import archive_tick
from .references import pack_snapshot, hydrate_rows


def window(now):
    end = now.replace(minute=0, second=0, microsecond=0)
    return end-timedelta(hours=1), end


def compact_cell(c):
    result = {k:deepcopy(c[k]) for k in ('value','status','reason','source','unit','observed_at','created_at',
        'record_id','metric','input_path','item_id','source_id','point_key','raw_value','density_label','entered_at',
        'retained_sample','raw_sample','key','alignment','derivation','definition_id') if k in c}
    effective = c.get('effective_density') or {}
    if effective.get('raw_sample'):
        result['raw_sample'] = deepcopy(effective['raw_sample'])
    sample = c.get('sample') or {}
    if sample:
        for key in ('batch_id','definition_id','quality','status_code','issue','source_at','server_at','received_at','raw_value'):
            result[key] = deepcopy(sample.get(key))
    if isinstance(c.get('components'),dict):
        result['components'] = {k:compact_cell(v) for k,v in c['components'].items()}
    elif isinstance(c.get('components'),list):
        result['components'] = [compact_cell(v) for v in c['components']]
    return result


def context_definition(result, store):
    group = gpt_group(store) or {}
    method = result['model'].get('method')
    model = group.get(method) or {}
    # Save coefficients/policies once per context, independent of mutable current model rows.
    definitions = {}
    def collect(c):
        sample = c.get('sample') or {}
        definition = sample.get('definition_id') or c.get('definition_id')
        if definition:
            identity = str([c.get('source_id'),c.get('point_key'),c.get('item_id')])
            definitions[identity] = dict(definition_id=definition,unit=c.get('unit'))
        parts = c.get('components') or []
        for part in parts.values() if isinstance(parts,dict) else parts:
            collect(part)
    for key,c in result['cells'].items():
        if method != 'expert8' or key != 'raw_ash':
            collect(c)
    sources = deepcopy(result['sources']['resolved_choices'])
    if method == 'expert8':
        sources.pop('raw-ash', None)  # Display-only source changes cannot invalidate advice.
    return dict(policy='decision-hourly-v2', evidence_policy=POLICY, segmentation=SEGMENT_POLICY,
        sources=sources,point_definitions=definitions,
        target=result['target'], model=result['model'], model_definition=deepcopy(model) if method in ('expert9','expert8') else
        {k:deepcopy(model[k]) for k in ('intercept','coefs','feature_names','featureNames','inputPolicy','inputBounds','imputeMeans') if k in model},
        density_guide=store.get('densityGuide') or {}, input_timing=deepcopy(result.get('input_timing')),
        input_derivations=deepcopy(result.get('input_derivations')))


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
                  for k,c in result['inputs'].items() if result['model'].get('method') != 'expert8' or k != 'raw_ash'}
    snapshot = {k:deepcopy(result[k]) for k in ('sources','density_label','model','target',
        'coarse_ash','heavy_amount','total_ash','suggested_density','guidance','warnings')}
    snapshot['cells'] = cells
    snapshot['input_timing'] = deepcopy(result.get('input_timing'))
    snapshot['input_derivations'] = deepcopy(result.get('input_derivations'))
    snapshot['evidence_policy'] = POLICY
    snapshot.update({k:deepcopy(result[k]) for k in ('features','expert_explanation') if k in result})
    snapshot['inputs'] = {k:compact_cell(c) for k,c in result['inputs'].items()}
    snapshot['density_channels'] = density_channels(db,now,compact_cell)
    snapshot['protection'] = {k:deepcopy(store.get(k)) for k in ('densityLastMoveAt','densityActionLatch')}
    row = DecisionCycleSample(slot=slot,sampled_at=iso_time(now),context_key=key,
        fingerprint=digest([key,identities]),snapshot=snapshot)
    db.add(row)
    observe(db,row)
    row.snapshot = pack_snapshot(db,snapshot,slot)
    db.flush()
    return True


def build_hour(db, start, end, now):
    # Only restore fields actually consumed by hour statistics. Full detail also
    # contains duplicate input/density-channel evidence and model explanations.
    fields = ('sources','model','input_timing','evidence_policy','total_ash',
              'suggested_density','cells','storage_version')
    # Parse each JSON once; separate SQLite JSON_EXTRACT expressions repeatedly
    # scan the same large document and cost more than the hydration they save.
    data = db.scalars(select(DecisionCycleSample).where(DecisionCycleSample.sampled_at>=iso_time(start),
        DecisionCycleSample.sampled_at<iso_time(end)).order_by(DecisionCycleSample.slot))
    rows = [SimpleNamespace(slot=r.slot,sampled_at=r.sampled_at,context_key=r.context_key,
        fingerprint=r.fingerprint,snapshot={**{k:r.snapshot[k] for k in fields if k in r.snapshot},
            'inputs':{k:v for k,v in r.snapshot.get('inputs',{}).items() if k=='is_stoppage'}}) for r in data]
    rows = hydrate_rows(db,rows)
    cells, sources, basis = historical_cells(db,start,end,rows)
    context = db.get(DecisionCycleContext,rows[-1].context_key) if rows else None
    step = (context.definition.get('density_guide') or {}).get('maxStep',.03) if context else .03
    advice, coverage, guide = hour_advice(rows,start,end,basis,step)
    evidence = summarize(rows,end)
    cells['total_ash'] = evidence['total_ash']
    guide.update(validation='awaiting_experiment', empirical_note=PROVISIONAL,
                 feasible_interval=None, interval_width_limit=None)
    advice['reason'] += '；'+PROVISIONAL
    cells['suggested_density'] = advice
    for key in ('coarse_ash',):
        if cells[key]['value'] is not None:
            cells[key]['reason'] += '；先逐次计算，再对结果求平均'
    return dict(policy='decision-hourly-v2', evidence=evidence, generated_at=iso_time(now),
        period=dict(start=iso_time(start),end=iso_time(end),apply_start=iso_time(end),apply_end=iso_time(end+timedelta(hours=1)),
            label=f'{start.astimezone(LOCAL):%m-%d %H:%M}–{end.astimezone(LOCAL):%H:%M}',
            apply_label=f'{end.astimezone(LOCAL):%H:%M}–{(end+timedelta(hours=1)).astimezone(LOCAL):%H:%M}'),
        cells=cells, sources=sources, density_label=sources['density_label'], target=dict(min=sources['target_min'],max=sources['target_max']),
        model=rows[-1].snapshot['model'] if rows else {}, context_key=rows[-1].context_key if rows else None,
        coverage=coverage,guidance=guide,density_basis=basis,
        coarse_ash=cells['coarse_ash'],heavy_amount=cells['heavy_amount'],total_ash=cells['total_ash'],suggested_density=advice,
        notes=['总灰分按 501+502 实际产煤量加权；每批最多计 5 秒，不跨缺口补齐产量。其他显示指标保留样本均值。',
               '手工数据按实际记录条数平均；没有新增时沿用该小时前最近值并显示采样时间。',
               '工况分段保留真实系统组合、均值、波动、方向和持续时间，暂不推断下一工况。',
               PROVISIONAL+'；等待响应实验，不限制未来估计区间宽度，单次执行仍限 ±0.03。'])


def observation_tick(now=None, sessions=SessionLocal):
    with sessions() as db:
        # Serialize duplicate worker ticks; one slot/one hour even during overlapping restarts.
        if db.bind.dialect.name == 'sqlite':
            db.execute(text('BEGIN IMMEDIATE'))
        now = now or utc_now()
        record(db,now)
        db.commit()
    return now


def summary_tick(now, sessions=SessionLocal):
    start,end = window(now)
    # WAL readers can calculate a full hour without holding up the collector.
    with sessions() as db:
        if db.bind.dialect.name == 'sqlite':
            db.execute(text('BEGIN'))
        if db.get(DecisionCycleHour,iso_time(start)):
            return
        snapshot = build_hour(db,start,end,now)
    with sessions() as db:
        if db.bind.dialect.name == 'sqlite':
            db.execute(text('BEGIN IMMEDIATE'))
        if not db.get(DecisionCycleHour,iso_time(start)):
            db.add(DecisionCycleHour(start=iso_time(start),end=iso_time(end),generated_at=iso_time(now),
                                    snapshot=snapshot))
        db.commit()


def maintenance_tick(now, sessions=SessionLocal):
    # Called only after a current observation commits: no preceding-hour record
    # can still be waiting for its write lock when we freeze the hour.
    summary_tick(now,sessions)
    start,_ = window(now)
    archive_tick(iso_time(start-timedelta(days=7)),sessions)
    from .ash_settlement import settlement_tick
    settlement_tick(now, sessions)


def tick(now=None, sessions=SessionLocal):
    """Synchronous entry point; the worker runs observation and maintenance separately."""
    maintenance_tick(observation_tick(now,sessions),sessions)


def read_hour(db, now=None, live=None, defer_missing=False):
    now = now or utc_now()
    start,end = window(now)
    saved = db.get(DecisionCycleHour,iso_time(start))
    if saved is None and defer_missing:
        # Browser polling must not launch duplicate full-hour calculations while
        # the background worker is preparing the newly completed hour.
        return dict(status='pending',checked_at=iso_time(now),
                    reason='上一小时统计正在生成，请稍候',
                    period=dict(start=iso_time(start),end=iso_time(end)))
    result = deepcopy(saved.snapshot) if saved else build_hour(db,start,end,now)
    refresh_coverage_policy(result)
    result['frozen'] = bool(saved)
    result['checked_at'] = iso_time(now)
    result['guidance'].update(validation='awaiting_experiment', empirical_note=PROVISIONAL,
                             feasible_interval=None, interval_width_limit=None)
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
