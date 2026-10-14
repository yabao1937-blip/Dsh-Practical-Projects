"""GPT hourly targets from revisable ash and authoritative prior-hour density.

This read-time view never rewrites the original cycle/hour decisions. Ash and
density intentionally describe different hours; both bases travel with the advice.
"""
from copy import deepcopy
from datetime import timedelta
from math import fsum
import json

from sqlalchemy import select

from ..hourly_brief.inputs import LOCAL, business_time, numeric
from ..telemetry.domain import iso_time
from ..telemetry.density_effective import DENSITY_ITEMS
from ..telemetry.models import TelemetryPoint, TelemetryReading
from ..telemetry.retained import density_rows, raw_sample, usable_at_receipt
from .ash_settlement import get_settlement, latest_end
from .evidence import PROVISIONAL
from .models import DecisionCycleContext, DecisionCycleHour, DecisionCycleSample
from .statistics import CADENCE, MIN_COVERAGE, valid
from .production_coverage import stopped_intervals, overlap, fields as coverage_fields
from ..gpt_density.execution import advise, setpoints, feedback_start
from ..hourly_brief.inputs import Inputs

POLICY = 'gpt-hourly-setpoint-v2'


def period(start, end):
    return dict(start=iso_time(start), end=iso_time(end),
                label=f'{start.astimezone(LOCAL):%m-%d %H:%M}–{end.astimezone(LOCAL):%H:%M}')


def density_mean(db, start, end, sources):
    """Mean retained events once; coverage is the union of at-most-5s intervals."""
    choice = (sources.get('resolved_choices') or {}).get('density-current')
    points = []
    if choice:
        source, key = json.loads(choice)
        points = density_rows(db, source) if key == 'a303_a304_density_average' else [db.get(TelemetryPoint, (source, key))]
    events, observed, definitions, reason = {}, set(), {}, ''
    if not points or not all(p and p.enabled for p in points):
        reason = '所选分选密度点不可用'
    else:
        parts = []
        for p in points:
            items = {}
            query = select(TelemetryReading).where(TelemetryReading.source_id == p.source_id,
                TelemetryReading.point_key == p.key, TelemetryReading.received_at >= iso_time(start),
                TelemetryReading.received_at < iso_time(end)).order_by(TelemetryReading.received_at, TelemetryReading.id)
            definitions[p.key] = set()
            for row in db.scalars(query):
                observed.add(row.received_at)
                s = raw_sample(row.sample) or {}
                if s.get('definition_id'):
                    definitions[p.key].add(s['definition_id'])
                cell = dict(value=s.get('value'), status='good' if usable_at_receipt(s, p.config) else 'missing')
                if valid(cell, 'density'):
                    items[(row.received_at, row.batch_id)] = cell['value']
            parts.append(items)
        if any(len(ids) > 1 for ids in definitions.values()):
            reason = '小时内密度采集点定义变化，暂不混合均值'
        for (stamp, batch), value in parts[0].items():
            if len(parts) == 1:
                events[stamp] = value
            elif batch and (stamp, batch) in parts[1]:
                events[stamp] = (value+parts[1][(stamp, batch)])/2
    stopped = stopped_intervals(db, start, end, sources)
    covered, cursor = 0., start.timestamp()
    active = {}
    stamps = sorted(observed)
    for i, stamp in enumerate(stamps):
        if stamp not in events:
            continue
        left = business_time(stamp).timestamp()
        right = min(end.timestamp(), left+CADENCE,
                    business_time(stamps[i+1]).timestamp() if i+1 < len(stamps) else end.timestamp())
        active_seconds = max(0., right-max(left, cursor)-overlap(max(left, cursor), right, stopped))
        covered += active_seconds
        if active_seconds:
            active[stamp] = events[stamp]
        cursor = max(cursor, right)
    value = fsum(active.values())/len(active) if active and not reason else None
    return dict(**period(start, end), value=value, status='average' if value is not None else 'missing',
        unit='g/cm³', source=sources.get('density_label'), choice=choice,
        sample_count=len(active), excluded_count=len(observed)-len(active),
        **coverage_fields(start, end, stopped, covered),
        observed_at=max(active) if active else None,
        reason=reason or ('上一完整小时有效实测密度的算术平均' if events else '上一完整小时没有有效实测密度'))


def context_keys(db, start, end, snapshot=None):
    if snapshot is None:
        saved = db.get(DecisionCycleHour, iso_time(start))
        snapshot = saved.snapshot if saved else {}
    keys = {s.get('state', {}).get('context_key') for s in (snapshot.get('evidence') or {}).get('segments', [])}
    keys.discard(None)
    if not keys:
        # Legacy summaries may predate segmented evidence; use original keys,
        # not the last key alone (a mid-hour change could have been reverted).
        keys = set(db.scalars(select(DecisionCycleSample.context_key).where(
            DecisionCycleSample.sampled_at >= iso_time(start), DecisionCycleSample.sampled_at < iso_time(end)).distinct()))
    if not keys and snapshot.get('context_key'):
        keys.add(snapshot['context_key'])
    return keys


def compatible_contexts(db, groups, current):
    definitions = [current]
    for keys in groups:
        if not keys:
            return False, '小时数据缺少来源与模型口径记录'
        for key in keys:
            row = db.get(DecisionCycleContext, key)
            if not row or not row.definition:
                return False, '小时数据缺少来源与模型口径记录'
            definitions.append(row.definition)
    # Targets are objectives, not measurement/model definitions. Reuse the
    # historical facts across target edits and recompute advice with today's goal.
    ignored = {'point_definitions', 'target'}
    expected = {k:v for k,v in current.items() if k not in ignored}
    known = {}
    for definition in definitions:
        if {k:v for k,v in definition.items() if k not in ignored} != expected:
            return False, '灰分小时、密度小时或当前的来源、模型或调密参数发生变化'
        # A temporarily missing input can omit its definition. Only conflicting
        # known definitions prove a real configuration change.
        for identity, value in definition.get('point_definitions', {}).items():
            if identity in known and known[identity] != value:
                return False, '灰分小时、密度小时或当前的采集点定义发生变化'
            known[identity] = value
    return True, ''


def current_problem(cell, key, now):
    if not valid(cell, key):
        return '不可用'
    if cell.get('status') == 'good':
        try:
            age = (now-business_time(cell['observed_at'])).total_seconds()
        except (KeyError, TypeError, ValueError):
            return '采集时间缺失或无效'
        if not -5 <= age <= 30:
            return '采集数据已过期或时钟异常'
    expected = 'g/cm³' if key == 'density' else ''
    if cell.get('unit', '') != expected:
        return '单位不正确'
    if key == 'density' and cell.get('item_id') not in (*DENSITY_ITEMS, 'a303_a304_density_average'):
        return '须选择实际密度采集点'
    if key == 'is_stoppage' and cell['value'] not in (0, 1):
        return '状态超出有效范围'
    return None


def apply_quality_advice(db, result, now, current, store, current_definition):
    # read_hour gives us a private copy; stored snapshots keep their original goal.
    result['statistical_target'] = deepcopy(result['target'])
    result['target'] = deepcopy(current['target'])
    result['advice_sources'] = deepcopy(current['sources'])
    start, end = business_time(result['period']['start']), business_time(result['period']['end'])
    ash_end = latest_end(now)
    ash_start = ash_end-timedelta(hours=1)
    settlement = get_settlement(db, ash_start, now)
    item = settlement['item']
    ash = {**period(ash_start, ash_end), **{k:deepcopy(item.get(k)) for k in
        ('value', 'coverage', 'valid_seconds', 'sample_count', 'provisional', 'aggregation', 'reason',
         'coverage_policy', 'stopped_seconds', 'expected_production_seconds', 'wall_coverage')},
        'revision':settlement['revision'], 'unit':'%'}
    density = density_mean(db, start, end, result['sources'])
    reasons = []
    blockers = []
    for key, basis, title in [('ash', ash, '小时灰分'), ('density', density, '平均密度')]:
        if not numeric(basis['value']):
            reasons.append(f'{title} {basis["label"]} 不可用：{basis["reason"]}')
            blockers.append(key)
        elif (basis.get('coverage') or 0) <= MIN_COVERAGE:
            reasons.append(f'{title} {basis["label"]} 有效覆盖 {basis["coverage"]:.1%}，需超过 {MIN_COVERAGE:.0%}')
            blockers.append(key)
    ok, config_reason = compatible_contexts(db,
        [context_keys(db, ash_start, ash_end), context_keys(db, start, end, result)], current_definition)
    if not ok:
        reasons.append(config_reason)
    cells = current.get('inputs') or current.get('cells') or {}
    if error := current_problem(cells.get('density', {}), 'density', now):
        reasons.append('当前分选密度'+error+'，暂缓调密')
    stop = cells.get('is_stoppage', {})
    if current.get('model', {}).get('method') in ('expert8', 'expert9') and (error := current_problem(stop, 'is_stoppage', now)):
        reasons.append('当前开停机状态'+error+'，暂缓调密')
    elif stop.get('value') == 1:
        reasons.append('当前设备停机或低负荷，暂缓调密')
    basis = current.get('execution_basis') or setpoints(Inputs(db, now, authoritative=True), cells.get('density', {}))
    guide = dict(rhoCur=basis['value'], rhoNew=None, deltaRho=None, direction='unavailable',
        maxStep=.03, advice_frozen=False, validation='awaiting_experiment', empirical_note=PROVISIONAL,
        feasible_interval=None, interval_width_limit=None)
    if numeric(ash['value']) and numeric(density['value']):
        heavy = settlement.get('heavy_ash') or {}
        records = heavy.get('records') or []
        # Retain assay/action provenance without using it as an execution gate.
        ends = [feedback_start(dict(status='manual', observed_at=r['observed_at'])) for r in records]
        evidence_end = min(ends) if ends and all(ends) else ash_start
        guide.update(advise(db, now, basis, ash['value'], result['target'], density['value'],
            evidence_end, store.get('densityGuide')))
        if guide['rhoNew'] is None:
            reasons.append(guide['reason'])
            blockers.append('setpoint')
    basis_text = (f'小时灰分 {ash["label"]}'+('（暂估）' if ash.get('provisional') else '')+
                  f'；工况平均密度 {density["label"]}；单次执行以当前设定为基准，最大 ±{guide["maxStep"]:.3f}')
    if reasons:
        guide.update(rhoNew=None, deltaRho=None, direction='unavailable',
                     execution_state='unavailable', channels={})
        explanation = '；'.join(reasons)
    else:
        explanation = guide['reason']
    reason = explanation+'；'+basis_text+'；'+PROVISIONAL
    guide.update(reason=reason, hold=bool(reasons), ash_basis=ash, density_basis=density)
    advice = dict(value=guide['rhoNew'], status='calculated' if guide['rhoNew'] is not None else 'missing',
                  unit='g/cm³', source='小时工况目标与当前设定密度', reason=reason)
    result.update(advice_policy=POLICY, advice_frozen=False, advice_inputs=dict(ash=ash, density=density),
        advice_blockers=blockers, guidance=guide, density_basis=density, execution_basis=basis, suggested_density=advice,
        coverage=dict(policy=POLICY, ratio=min(ash.get('coverage') or 0, density['coverage']),
            ash_ratio=ash.get('coverage') or 0, density_ratio=density['coverage'], required_ratio=MIN_COVERAGE,
            required_ratio_exclusive=True, max_gap_seconds=None))
    result['cells']['suggested_density'] = advice
    result['cells']['density'] = deepcopy(density)
    result['notes'] = [n for n in result.get('notes', []) if '单次执行' not in n]
    result['notes'].append('小时工况目标采用最新发布灰分与上一完整小时实测均值；正式执行相对当前设定每路限 ±0.03。确认停产无产出的时段不计入生产覆盖分母，未知时段仍算缺口。')
    return result
