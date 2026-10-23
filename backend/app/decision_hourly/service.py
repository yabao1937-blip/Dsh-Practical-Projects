"""Forward-only decision archive and immutable previous-hour summaries."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import logging
import time
import json

from sqlalchemy import select, text, func

from ..database import SessionLocal
from ..hourly_brief.inputs import LOCAL, business_time
from ..services.decision_sources import shared_snapshot
from ..services.decision_live import gpt_group
from ..services.state import load_store
from ..telemetry.domain import iso_time, utc_now
from .models import DecisionCycleContext, DecisionCycleSample, DecisionCycleHour
from .statistics import CADENCE, ARCHIVE_CADENCE, digest, hour_advice
from .history import historical_cells
from .evidence import POLICY, PROVISIONAL, SEGMENT_POLICY, summarize, weights
from .tracking import density_channels, observe
from .archive import archive_tick
from .references import pack_snapshot, hydrate_rows, pin_readings

log = logging.getLogger(__name__)


def window(now):
    end = now.replace(minute=0, second=0, microsecond=0)
    return end-timedelta(hours=1), end


def compact_cell(c):
    result = {k:deepcopy(c[k]) for k in ('value','status','reason','source','unit','observed_at','created_at',
        'record_id','metric','input_path','item_id','source_id','point_key','raw_value','density_label','entered_at',
        'retained_sample','raw_sample','key','alignment','derivation','definition_id',
        'entered_observed_at','time_basis') if k in c}
    effective = c.get('effective_density') or {}
    if effective.get('raw_sample'):
        result['raw_sample'] = deepcopy(effective['raw_sample'])
    sample = c.get('sample') or {}
    if sample:
        for key in ('batch_id','definition_id','quality','status_code','issue','source_at','server_at','received_at','raw_value','cadence_seconds'):
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
    if result.get('execution_basis'):
        collect(result['execution_basis'])
    sources = deepcopy(result['sources']['resolved_choices'])
    if method == 'expert8':
        sources.pop('raw-ash', None)  # Display-only source changes cannot invalidate advice.
    return dict(policy='decision-hourly-v2', evidence_policy=POLICY, segmentation=SEGMENT_POLICY,
        sources=sources,point_definitions=definitions,
        target=result['target'], model=result['model'], model_definition=deepcopy(model) if method in ('expert9','expert8') else
        {k:deepcopy(model[k]) for k in ('intercept','coefs','feature_names','featureNames','inputPolicy','inputBounds','imputeMeans') if k in model},
        density_guide=store.get('densityGuide') or {}, input_timing=deepcopy(result.get('input_timing')),
        input_derivations=deepcopy(result.get('input_derivations')))


def prepare_record(db, now, result=None, store=None, *, cadence_seconds=CADENCE):
    """Prepare detached evidence; no writes or writer reservation are required."""
    slot = int(now.timestamp())//CADENCE
    if cadence_seconds == ARCHIVE_CADENCE:
        first = int(now.timestamp())//ARCHIVE_CADENCE*ARCHIVE_CADENCE//CADENCE
        if db.scalar(select(DecisionCycleSample.slot).where(DecisionCycleSample.slot >= first,
                DecisionCycleSample.slot < first+ARCHIVE_CADENCE//CADENCE).limit(1)) is not None:
            return None
    if db.get(DecisionCycleSample,slot):
        return None
    result = result or shared_snapshot(db)
    # A lock wait/computation crossing an hour cannot assign new inputs to the old hour.
    now = business_time(result['generated_at'])
    slot = int(now.timestamp())//CADENCE
    if db.get(DecisionCycleSample,slot):
        return None
    store = store if store is not None else load_store(db)
    context = context_definition(result,store)
    key = digest(context)
    cells = {k:compact_cell(c) for k,c in result['cells'].items()}
    identities = {k:{f:c.get(f) for f in ('value','status','observed_at','entered_at','source_id','item_id','batch_id')}
                  for k,c in result['inputs'].items() if result['model'].get('method') != 'expert8' or k != 'raw_ash'}
    snapshot = {k:deepcopy(result[k]) for k in ('sources','density_label','model','target',
        'coarse_ash','heavy_amount','total_ash','suggested_density','guidance','warnings')}
    snapshot['execution_basis'] = deepcopy(result.get('execution_basis'))
    snapshot['cells'] = cells
    snapshot['input_timing'] = deepcopy(result.get('input_timing'))
    snapshot['input_derivations'] = deepcopy(result.get('input_derivations'))
    snapshot['evidence_policy'] = POLICY
    snapshot['cadence_seconds'] = cadence_seconds
    snapshot.update({k:deepcopy(result[k]) for k in ('features','expert_explanation') if k in result})
    snapshot['inputs'] = {k:compact_cell(c) for k,c in result['inputs'].items()}
    quality_sources = set()
    def source_clock(c):
        if c.get('source_id') and c.get('point_key') and c.get('source_id') != 'manual-measurements':
            lag = (c.get('alignment') or {}).get('level_lag_seconds',0)
            quality_sources.add((c['source_id'],c['point_key'],lag if lag in (0,90) else 0))
        parts = c.get('components') or []
        for part in parts.values() if isinstance(parts,dict) else parts:
            source_clock(part)
    for name,c in result['inputs'].items():
        if result['model'].get('method') != 'expert8' or name != 'raw_ash':
            source_clock(c)
    snapshot['quality_sources'] = [dict(source_id=s,point_key=p,lag_seconds=lag)
        for s,p,lag in sorted(quality_sources)]
    snapshot['density_channels'] = density_channels(db,now,compact_cell)
    snapshot['protection'] = {k:deepcopy(store.get(k)) for k in ('densityLastMoveAt','densityActionLatch')}
    pins = set()
    packed = pack_snapshot(db, snapshot, slot, pins=pins)
    return dict(slot=slot, now=now, result=result, context=context, key=key,
                fingerprint=digest([key,identities]), snapshot=snapshot, packed=packed, pins=pins)


def save_record(db, prepared):
    """Short serialized commit; historical reads and calculations happen before it."""
    now, slot, key = prepared['now'], prepared['slot'], prepared['key']
    if db.get(DecisionCycleSample, slot):
        return False
    if prepared['snapshot'].get('cadence_seconds') == ARCHIVE_CADENCE:
        first = slot//(ARCHIVE_CADENCE//CADENCE)*(ARCHIVE_CADENCE//CADENCE)
        if db.scalar(select(DecisionCycleSample.slot).where(DecisionCycleSample.slot >= first,
                DecisionCycleSample.slot < first+ARCHIVE_CADENCE//CADENCE).limit(1)) is not None:
            return False
    latest_slot = db.scalar(select(func.max(DecisionCycleSample.slot)))
    if latest_slot is not None and latest_slot > slot:
        return False  # A slower concurrent preparation must not rewind observation tracking.
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    if db.get(DecisionCycleHour, iso_time(current_hour)):
        return False
    from ..gpt_density.hourly_anchor import capture
    capture(db, now, prepared['result'], prepared['context'])
    if not db.get(DecisionCycleContext, key):
        db.add(DecisionCycleContext(key=key, definition=prepared['context']))
        db.flush()
    row = DecisionCycleSample(slot=slot, sampled_at=iso_time(now), context_key=key,
        fingerprint=prepared['fingerprint'], snapshot=prepared['snapshot'])
    db.add(row)
    observe(db,row)
    pin_readings(db, prepared['pins'], slot)
    row.snapshot = prepared['packed']
    db.flush()
    return True


def record(db, now, result=None, store=None):
    prepared = prepare_record(db, now, result, store)
    return save_record(db, prepared) if prepared is not None else False


def build_hour(db, start, end, now):
    # Only restore fields actually consumed by hour statistics. Full detail also
    # contains duplicate input/density-channel evidence and model explanations.
    fields = ('sources','model','input_timing','evidence_policy','total_ash',
              'suggested_density','cells','storage_version','cadence_seconds','quality_sources')
    # Parse each JSON once; separate SQLite JSON_EXTRACT expressions repeatedly
    # scan the same large document and cost more than the hydration they save.
    data = db.scalars(select(DecisionCycleSample).where(DecisionCycleSample.sampled_at>=iso_time(start),
        DecisionCycleSample.sampled_at<iso_time(end)).order_by(DecisionCycleSample.slot))
    rows = [SimpleNamespace(slot=r.slot,sampled_at=r.sampled_at,context_key=r.context_key,
        fingerprint=r.fingerprint,snapshot={**{k:r.snapshot[k] for k in fields if k in r.snapshot},
            'inputs':{k:v for k,v in r.snapshot.get('inputs',{}).items() if k=='is_stoppage'}}) for r in data]
    rows = hydrate_rows(db,rows)
    durations = weights(rows,end,db=db)
    cells, sources, basis = historical_cells(db,start,end,rows,weights=durations)
    context = db.get(DecisionCycleContext,rows[-1].context_key) if rows else None
    step = (context.definition.get('density_guide') or {}).get('maxStep',.03) if context else .03
    advice, coverage, guide = hour_advice(rows,start,end,basis,step,weights=durations)
    evidence = summarize(rows,end,prepared_weights=durations)
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
        notes=['总灰分按 501+502 实际产煤量加权；每批按记录的 5/30 秒时长计权，不跨缺口补齐产量。其他显示指标按有效时长加权。',
               '手工数据按实际记录条数平均；没有新增时沿用该小时前最近值并显示采样时间。',
               '工况分段保留真实系统组合、均值、波动、方向和持续时间，暂不推断下一工况。',
               PROVISIONAL+'；等待响应实验，不限制未来估计区间宽度，单次执行仍限 ±0.03。'])


def periodic_batch_ready(db, now, sources=None):
    """Wait for this bucket's durable batch, including bad-quality batches.

    Reading the five-second runtime cache here would leave dangling archive
    references. Never turn a previous bucket or a quality event into a new period.
    """
    from datetime import datetime, timezone
    from ..telemetry.models import TelemetryReading
    start = datetime.fromtimestamp(int(now.timestamp())//ARCHIVE_CADENCE*ARCHIVE_CADENCE,timezone.utc)
    query = select(TelemetryReading.source_id,TelemetryReading.sample).where(
        TelemetryReading.received_at >= iso_time(start),TelemetryReading.received_at <= iso_time(now))
    if sources:
        query = query.where(TelemetryReading.source_id.in_(sources))
    found = {source for source,sample in db.execute(query) if sample.get('storage_kind') == 'periodic'
             and sample.get('cadence_seconds') == ARCHIVE_CADENCE and not sample.get('runtime_only')}
    return set(sources) <= found if sources else bool(found)


def observation_tick(now=None, sessions=SessionLocal, *, cadence_seconds=CADENCE):
    from .observation_guard import configuration_revision
    explicit_time = now is not None
    started = time.monotonic()
    with sessions() as db:
        if db.bind.dialect.name == 'sqlite':
            db.execute(text('BEGIN'))
        revision = configuration_revision(db)
        if cadence_seconds == ARCHIVE_CADENCE and not periodic_batch_ready(db,now or utc_now()):
            return None
        prepared = prepare_record(db, now or utc_now(), cadence_seconds=cadence_seconds)
        if prepared and cadence_seconds == ARCHIVE_CADENCE:
            sources = set()
            for choice in prepared['snapshot'].get('sources',{}).get('resolved_choices',{}).values():
                try:
                    source,_ = json.loads(choice)
                except (ValueError,TypeError):
                    continue
                if source != 'manual-measurements':
                    sources.add(source)
            if not periodic_batch_ready(db,prepared['now'],sources):
                return None
    log.info('decision_observation phase=prepare elapsed_ms=%.2f', (time.monotonic()-started)*1000)
    if prepared is None:
        return None
    started = time.monotonic()
    acquired, outcome = None, 'failed'
    try:
        with sessions() as db:
            # Serialize duplicate worker ticks; one slot/one hour even during overlapping restarts.
            if db.bind.dialect.name == 'sqlite':
                db.execute(text('BEGIN IMMEDIATE'))
            acquired = time.monotonic()
            # A discarded preparation must not ask maintenance to freeze its hour.
            if configuration_revision(db) != revision:
                outcome = 'configuration_changed'
                return None
            if not explicit_time and window(utc_now()) != window(prepared['now']):
                outcome = 'hour_changed'
                return None
            if not save_record(db, prepared):
                outcome = 'already_saved_or_frozen'
                return None
            db.commit()
            outcome = 'committed'
    finally:
        ended = time.monotonic()
        log.info('decision_observation phase=commit outcome=%s lock_wait_ms=%.2f write_ms=%.2f',
                 outcome, ((acquired or ended)-started)*1000,
                 (ended-acquired)*1000 if acquired is not None else 0)
    return prepared['now']


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
    observed = observation_tick(now,sessions)
    if observed is not None:
        maintenance_tick(observed,sessions)


def read_hour(db, now=None, live=None, defer_missing=False, *, store_loader=None, settlement_reader=None):
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
    result['frozen'] = bool(saved)
    result['checked_at'] = iso_time(now)
    from .quality_advice import apply_quality_advice
    current = live if live is not None else (shared_snapshot(db, realtime=True) if store_loader is None
        else shared_snapshot(db, store_loader=store_loader, realtime=True))
    loader = load_store if store_loader is None else store_loader
    store = loader(db)
    if settlement_reader is None:
        return apply_quality_advice(db, result, now, current, store, context_definition(current, store))
    return apply_quality_advice(db, result, now, current, store, context_definition(current, store),
                               settlement_reader=settlement_reader)
