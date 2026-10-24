"""GPT-only, forward-only operating evidence. No response fitting or device writes."""
from collections import Counter
from datetime import datetime, timezone, timedelta
from math import fsum, sqrt
from bisect import bisect_right
from copy import deepcopy

from ..hourly_brief.inputs import business_time, numeric
from ..telemetry.domain import iso_time
from .statistics import CADENCE, cadence_seconds, row_seconds, valid, digest

POLICY = 'gpt-operating-evidence-v1'
MASS = 'output-mass-v1'
# Initial segmentation settings, not experimentally validated process boundaries.
SEGMENT_POLICY = dict(version=POLICY, confirm_samples=3,
    thresholds=dict(raw_ash=3.0, coal_amount=50.0, level=5.0, density=.01),
    note='初始分段阈值：灰分 3 个百分点、入洗量 50 t/h、液位 5、密度 0.01；连续 3 次确认。')
FACTORS = ('raw_ash', 'coal_amount', 'level', 'density', 'heavy_ash', 'float_ash',
           'coarse_ash', 'coarse_amount', 'heavy_amount', 'float_amount', 'total_ash')
PROVISIONAL = '专家经验建议 · 非实验验证的达标区间'


def at(row):
    return business_time(row.sampled_at)


def stamp(seconds):
    return iso_time(datetime.fromtimestamp(seconds, timezone.utc))


def flow_event(row):
    """Same received batch only; retained/default/manual flows are not production evidence."""
    if row.snapshot.get('evidence_policy') != POLICY:
        return None, '旧口径明细'
    cells = row.snapshot['cells']
    a, b = (cells.get(k, {}) for k in ('belt501', 'belt502'))
    if any(c.get('status') != 'good' or not numeric(c.get('value')) or c['value'] < 0
           or c.get('unit') != 't/h' or c.get('quality') != 'good' for c in (a, b)):
        return None, '皮带流量缺失或质量异常'
    identity = ('source_id', 'batch_id', 'observed_at')
    if any(not a.get(k) or a.get(k) != b.get(k) for k in identity):
        return None, '501/502 不同源或不同批次'
    if not all(c.get('definition_id') for c in (a, b)):
        return None, '缺少点位定义'
    age = (at(row)-business_time(a['observed_at'])).total_seconds()
    if not 0 <= age <= CADENCE*2:
        return None, '流量读数过期或晚于决策'
    # Conservative boundary: never carry a previous-hour batch into this hour.
    # The former hour already credited its declared period (clipped at its end).
    # Without a new batch the uncovered boundary remains a gap, not new production.
    if business_time(a['observed_at']) < at(row).replace(minute=0,second=0,microsecond=0):
        return None, '跨小时旧皮带批次'
    key = digest([*(a[k] for k in identity), a['definition_id'], b['definition_id']])
    return (key, a['value']+b['value']), None


def quality_boundaries(db, rows, end):
    """Rare durable quality events can terminate a saved 30/60s period."""
    if db is None:
        return {}
    from sqlalchemy import select, tuple_
    from ..telemetry.models import TelemetryReading
    identities = {(c.get('source_id'),c.get('point_key')) for r in rows
        if cadence_seconds(r.snapshot) > CADENCE for c in quality_sources(r)
        if c.get('source_id') and c.get('point_key')}
    if not identities:
        return {}
    query = select(TelemetryReading.source_id,TelemetryReading.point_key,
        TelemetryReading.received_at,TelemetryReading.sample).where(
        tuple_(TelemetryReading.source_id,TelemetryReading.point_key).in_(identities),
        TelemetryReading.received_at >= iso_time(min(at(r) for r in rows)-timedelta(seconds=90)),
        TelemetryReading.received_at < iso_time(end)).order_by(TelemetryReading.received_at)
    result = {}
    for source,key,when,sample in db.execute(query):
        if (sample.get('storage_kind') == 'quality-event' and
                (sample.get('quality') != 'good' or sample.get('issue'))):
            result.setdefault((source,key),[]).append(business_time(when).timestamp())
    return result


def quality_sources(row):
    return [*(row.snapshot.get('quality_sources') or []),
        *(row.snapshot.get('cells',{}).get(k,{}) for k in ('belt501','belt502'))]


def weights(rows, end, *, db=None):
    """Each archived batch covers its explicit 5/30/60s cadence, never a gap/hour."""
    if rows and all(hasattr(row, '_settlement_weight') for row in rows):
        # Permanent chemistry frames carry the original quality-event clipping.
        # They are not telemetry and must not consult deleted raw history.
        return {row.slot: deepcopy(row._settlement_weight) for row in rows}
    seen, result = set(), {}
    boundaries = quality_boundaries(db,rows,end)
    for i, row in enumerate(rows):
        event, reason = flow_event(row)
        seconds = row_seconds(row, end, rows[i+1] if i+1 < len(rows) else None)
        if cadence_seconds(row.snapshot) > CADENCE:
            left = at(row).timestamp()
            for c in quality_sources(row):
                times = boundaries.get((c.get('source_id'),c.get('point_key')),[])
                lag = c.get('lag_seconds',0)
                lag = lag if lag in (0,90) else 0
                i_boundary = bisect_right(times,left-lag)
                if i_boundary < len(times):
                    seconds = min(seconds,times[i_boundary]+lag-left)
        mass = 0.
        if event:
            key, rate = event
            if key in seen:
                reason = '重复皮带批次'
            else:
                seen.add(key)
                mass = rate*seconds/3600
        result[row.slot] = dict(seconds=seconds if reason is None else 0., duration_seconds=seconds,
                                mass_t=mass, reason=reason)
    return result


def mass_cell(rows, w):
    mass, ash_mass, observed, duration, flow_duration, accepted = [], [], [], [], [], []
    excluded = Counter()
    for row in rows:
        weight = w[row.slot]
        if weight['reason']:
            excluded[weight['reason']] += 1
            continue
        observed.append(weight['mass_t'])
        flow_duration.append(weight['seconds'])
        c = row.snapshot.get('total_ash', {})
        if not valid(c) or not 0 <= c['value'] <= 100:
            excluded['无有效总灰分'] += 1
            continue
        if weight['mass_t'] <= 0:
            excluded['零产出'] += 1
            continue
        mass.append(weight['mass_t'])
        ash_mass.append(c['value']/100*weight['mass_t'])
        duration.append(weight['seconds'])
        accepted.append(row)
    tonnes, ashes = fsum(mass), fsum(ash_mass)
    return dict(value=100*ashes/tonnes if tonnes else None,
        status='average' if tonnes else 'missing', unit='%', aggregation=MASS,
        sample_count=len(accepted), excluded_count=sum(excluded.values()), excluded_reasons=dict(excluded),
        mass_t=tonnes, ash_mass_t=ashes, observed_mass_t=fsum(observed),
        valid_seconds=fsum(duration), flow_seconds=fsum(flow_duration),
        observed_at=accepted[-1].sampled_at if accepted else None,
        reason=f'按实际产煤量加权，灰分配对产量 {tonnes:.3f} t；501+502 流量 × 有效秒数 / 3600' if tonnes else '无有效灰分与实际产煤量配对记录',
        source='逐次三组分质量平衡 × 501+502 实际产煤量')


def state(row):
    cells = row.snapshot['cells']
    switches = {key: {label: c.get('value') if c.get('status') == 'good' else None
                     for label, c in (cells.get(key, {}).get('components') or {}).items()}
                for key in ('systems', 'desliming', 'float_press')}
    stop = (row.snapshot.get('inputs', {}).get('is_stoppage') or {}).get('value')
    return dict(context_key=row.context_key, switches=switches, stopped=stop,
                manual_basis={key:cells[key].get('value') for key in FACTORS if
                              cells.get(key,{}).get('status') in ('manual','historical','default')},
                quality={key: cells.get(key, {}).get('status') for key in FACTORS})


def statistics(rows, key, w):
    accepted = [(r, r.snapshot['cells'].get(key, {})) for r in rows
                if valid(r.snapshot['cells'].get(key, {}), key)]
    if not accepted:
        return dict(mean=None, mass_mean=None, count=0)
    values = [c['value'] for _, c in accepted]
    weighted = any(cadence_seconds(r.snapshot) > CADENCE for r,_ in accepted)
    actual_durations = [w[r.slot].get('duration_seconds',w[r.slot]['seconds']) for r,_ in accepted]
    durations = actual_durations if weighted else [1.]*len(accepted)
    seconds = fsum(durations)
    avg = fsum(v*d for v,d in zip(values,durations))/seconds if seconds else fsum(values)/len(values)
    tonnes = fsum(w[r.slot]['mass_t'] for r, _ in accepted)
    delta = values[-1]-values[0]
    return dict(mean=avg, mass_mean=fsum(c['value']*w[r.slot]['mass_t'] for r,c in accepted)/tonnes if tonnes else None,
        stddev=sqrt(fsum((v-avg)**2*d for v,d in zip(values,durations))/seconds) if seconds else 0., minimum=min(values), maximum=max(values),
        first=values[0], last=values[-1], change=delta,
        direction='上升' if delta > 1e-9 else '下降' if delta < -1e-9 else '平稳',
        count=len(values), valid_seconds=fsum(actual_durations), unit=accepted[0][1].get('unit',''),
        manual_record_ids=sorted({c['record_id'] for _,c in accepted if c.get('metric') and c.get('record_id')}))


def summarize(rows, end, *, db=None, prepared_weights=None):
    rows = sorted(rows, key=lambda r: r.slot)
    w = weights(rows, end, db=db) if prepared_weights is None else prepared_weights
    segments, current, previous, pending, runs = [], [], None, None, 0

    def finish(reason,boundary=None):
        if not current:
            return
        last = current[-1]
        stop = min(end.timestamp(), at(last).timestamp()+cadence_seconds(last.snapshot),
                   boundary.timestamp() if boundary else end.timestamp())
        segments.append(dict(id=digest([POLICY,current[0].slot,last.slot,current[0].context_key]),
            start=current[0].sampled_at, end=stamp(stop),
            duration_seconds=stop-at(current[0]).timestamp(), sample_count=len(current),
            valid_seconds=fsum(w[r.slot]['seconds'] for r in current),
            start_reason=reason, state=state(current[0]), first_slot=current[0].slot, last_slot=last.slot,
            factors={k: statistics(current,k,w) for k in FACTORS}, total_ash=mass_cell(current,w)))

    segment_reason = '小时起点'
    for row in rows:
        reason = None
        if previous and (at(row)-at(previous)).total_seconds() > cadence_seconds(previous.snapshot)+1:
            reason = '采集缺口'
        elif previous and state(row) != state(previous):
            reason = '系统组合、开停机、补录采用值、质量或计算口径变化'
        elif current:
            changes = []
            for key, threshold in SEGMENT_POLICY['thresholds'].items():
                cell = row.snapshot['cells'].get(key,{})
                baseline = [r.snapshot['cells'].get(key,{}) for r in current]
                values = [c['value'] for c in baseline if valid(c,key)]
                if values and valid(cell,key):
                    delta = cell['value']-fsum(values)/len(values)
                    if abs(delta) >= threshold:
                        changes.append((key, 1 if delta>0 else -1))
            signature = tuple(changes)
            runs = runs+1 if signature and signature == pending else 1 if signature else 0
            pending = signature
            if runs >= SEGMENT_POLICY['confirm_samples']:
                reason = '明显变化持续确认：'+ '、'.join(key for key,_ in changes)
        if reason:
            finish(segment_reason,at(row))
            current, pending, runs, segment_reason = [], None, 0, reason
        current.append(row)
        previous = row
    finish(segment_reason)
    return dict(policy=POLICY, segmentation=SEGMENT_POLICY, segments=segments,
        total_ash=mass_cell(rows,w),
        response=dict(status='awaiting_experiment', interval=None, interval_width_limit=None,
            execution_step_limit=.03, assumed_delay_seconds=0, note=PROVISIONAL))
