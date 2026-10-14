"""Two-hour delayed ash settlement, using frozen process data and released assays.

This is a retrospective quality statistic, not a replay of an online decision.
The original coarse prediction, hour advice and cycle snapshots are immutable.
"""
from collections import Counter
from copy import deepcopy
from datetime import timedelta
import json
from math import fsum
from types import SimpleNamespace

from sqlalchemy import select, text

from ..hourly_brief.inputs import LOCAL, business_time, numeric
from ..models import ManualMeasurement
from ..services.density import calc_total_ash
from ..telemetry.domain import iso_time, utc_now
from .archive import archived_rows
from .ash_history import read_ash_hours
from .evidence import MASS, POLICY as EVIDENCE_POLICY, at, weights
from .models import DecisionAshSettlement, DecisionCycleHour, DecisionCycleSample
from .references import resolve_snapshots
from .statistics import CADENCE, valid

POLICY = 'gpt-ash-settlement-v1'
DELAY = timedelta(hours=2)
LEGACY = 'legacy-sample-mean'


def latest_end(now):
    return (now.astimezone(LOCAL)-DELAY).replace(minute=0, second=0, microsecond=0)


def load_rows(db, start):
    end = start+timedelta(hours=1)
    rows = {r['slot']: SimpleNamespace(**r) for r in archived_rows(db, iso_time(start))}
    hot = list(db.scalars(select(DecisionCycleSample).where(DecisionCycleSample.sampled_at >= iso_time(start),
        DecisionCycleSample.sampled_at < iso_time(end)).order_by(DecisionCycleSample.slot)))
    # Resolve only ash/flow evidence. Models, density channels and other telemetry
    # are not replayed and need not be hydrated for retrospective ash settlement.
    snapshots = []
    for row in hot:
        s = row.snapshot
        snapshots.append({**{k:s[k] for k in ('storage_version', 'evidence_policy', 'sources',
            'coarse_ash', 'heavy_amount', 'total_ash') if k in s},
            'cells':{k:v for k,v in s.get('cells', {}).items() if k in
                     ('belt501', 'belt502', 'float_ash', 'float_amount', 'coarse_amount')}})
    resolved = resolve_snapshots(db, snapshots)
    rows.update({r.slot: SimpleNamespace(slot=r.slot, sampled_at=r.sampled_at,
        fingerprint=r.fingerprint, snapshot=s) for r,s in zip(hot, resolved)})
    return sorted(rows.values(), key=lambda r: r.slot)


def manual_basis(db, start, end):
    deadline = end+DELAY
    query = select(ManualMeasurement).where(ManualMeasurement.metric == 'heavy_clean_coal_ash',
        ManualMeasurement.unit == '%', ManualMeasurement.value >= 0, ManualMeasurement.value <= 100,
        ManualMeasurement.created_at <= iso_time(deadline), ManualMeasurement.observed_at < iso_time(end))
    records = list(db.scalars(query.where(ManualMeasurement.observed_at >= iso_time(start))
                             .order_by(ManualMeasurement.observed_at, ManualMeasurement.id)))
    carried = not records
    if carried:
        record = db.scalar(query.where(ManualMeasurement.observed_at < iso_time(start))
                           .order_by(ManualMeasurement.observed_at.desc(), ManualMeasurement.id.desc()).limit(1))
        records = [record] if record else []
    return dict(value=fsum(r.value for r in records)/len(records) if records else None,
        carried=carried, records=[dict(id=r.id, observed_at=r.observed_at, created_at=r.created_at, value=r.value)
                                 for r in records], deadline=iso_time(deadline))


def permits_manual(snapshot):
    choice = (snapshot.get('sources', {}).get('resolved_choices') or {}).get('heavy-ash')
    if not choice:  # Automatic selection gives manual assays priority.
        return True
    try:
        source, point = json.loads(choice)
    except (ValueError, TypeError):
        return False
    return source == 'manual-measurements' and point == 'heavy_clean_coal_ash'


def replaced_total(row, heavy_ash):
    s, cells = row.snapshot, row.snapshot.get('cells', {})
    needed = dict(float_ash=cells.get('float_ash', {}), float_amount=cells.get('float_amount', {}),
                  coarse_amount=cells.get('coarse_amount', {}), coarse_ash=s.get('coarse_ash', {}),
                  heavy_amount=s.get('heavy_amount', {}))
    for key, cell in needed.items():
        if not valid(cell) or cell['value'] < 0 or ('ash' in key and cell['value'] > 100):
            return None, '原始'+key+'缺失或质量无效'
    f = needed['float_ash']
    if f.get('unit') != '%':
        return None, '原始浮精灰分单位无效'
    if f['status'] == 'good':
        try:
            age = (at(row)-business_time(f['observed_at'])).total_seconds()
        except (KeyError, ValueError, TypeError):
            return None, '原始浮精灰分采集时间无效'
        if not -5 <= age <= 30:
            return None, '原始浮精灰分采集过期'
    h, fa, ca = (needed[k]['value'] for k in ('heavy_amount', 'float_amount', 'coarse_amount'))
    total_rate = cells['belt501']['value']+cells['belt502']['value']
    if abs(h+fa+ca-total_rate) > max(1e-6, total_rate*1e-9):
        return None, '原始组分煤量不平衡'
    result = calc_total_ash(heavy_ash, h, f['value'], fa, needed['coarse_ash']['value'], ca)
    return (result, None) if numeric(result) and 0 <= result <= 100 else (None, '结算总灰分无效')


def contribution_item(contributions, start, end):
    mass, ash_mass, legacy, seconds, count = [], [], [], [], 0
    for c in contributions:
        when = business_time(c['sampled_at'])
        overlap = max(0., (min(end, when+timedelta(seconds=c['seconds']))-max(start, when)).total_seconds())
        if c['aggregation'] == LEGACY:
            if start <= when < end:
                legacy.append(c['value'])
                seconds.append(overlap)
                count += 1
        elif overlap:
            fraction = overlap/c['seconds']
            mass.append(c['mass_t']*fraction)
            ash_mass.append(c['ash_mass_t']*fraction)
            seconds.append(overlap)
            count += 1
    tonnes, ashes = fsum(mass), fsum(ash_mass)
    mixed = bool(tonnes and legacy)
    duration, covered = (end-start).total_seconds(), fsum(seconds)
    value = None if mixed else 100*ashes/tonnes if tonnes else fsum(legacy)/len(legacy) if legacy else None
    return dict(start=iso_time(start), end=iso_time(end), value=value, sample_count=count,
        expected_count=int(duration/CADENCE), coverage=min(1., covered/duration), valid_seconds=covered,
        mass_t=tonnes, ash_mass_t=ashes, legacy_sum=fsum(legacy), legacy_count=len(legacy),
        aggregation='mixed' if mixed else MASS if tonnes or not legacy else LEGACY,
        status='missing' if value is None else 'complete' if covered >= duration-1e-6 else 'partial',
        reason='新旧口径不能合并' if mixed else '按原时段实际产煤量加权' if tonnes else
               '旧口径样本均值' if legacy else '原时段无有效灰分与实际产量配对记录')


def build_settlement(db, start):
    end = start+timedelta(hours=1)
    basis, rows = manual_basis(db, start, end), load_rows(db, start)
    w, contributions, excluded, replaced = weights(rows, end), [], Counter(), 0
    seen_legacy = set()
    for row in rows:
        use_manual = basis['value'] is not None and permits_manual(row.snapshot)
        if row.snapshot.get('evidence_policy') != EVIDENCE_POLICY:
            old = row.snapshot.get('total_ash', {})
            if row.fingerprint not in seen_legacy and valid(old) and 0 <= old['value'] <= 100:
                contributions.append(dict(slot=row.slot, sampled_at=row.sampled_at, seconds=CADENCE,
                    aggregation=LEGACY, value=old['value'], manual_applied=False, legacy_reference=True))
            seen_legacy.add(row.fingerprint)
            continue
        weight = w[row.slot]
        if weight['reason'] or weight['mass_t'] <= 0:
            excluded[weight['reason'] or '零产出'] += 1
            continue
        if use_manual:
            value, reason = replaced_total(row, basis['value'])
        else:
            old = row.snapshot.get('total_ash', {})
            value = old.get('value') if valid(old) and 0 <= old['value'] <= 100 else None
            reason = '原始总灰分无效' if value is None else None
        if reason:
            excluded[reason] += 1
            continue
        replaced += int(use_manual)
        contributions.append(dict(slot=row.slot, sampled_at=row.sampled_at, seconds=weight['seconds'],
            aggregation=MASS, mass_t=weight['mass_t'], ash_mass_t=value/100*weight['mass_t'],
            manual_applied=use_manual))
    item = contribution_item(contributions, start, end)
    item['legacy_reference'] = any(c.get('legacy_reference') for c in contributions)
    # A frozen mean with no details remains a reference, never a reconstructed result.
    if not rows:
        old = read_ash_hours(db, start, end, now=end)['items'][0]
        if old['value'] is not None:
            item = dict(old, legacy_count=old['sample_count'] if old['aggregation'] == LEGACY else 0,
                        legacy_sum=old['value']*old['sample_count'] if old['aggregation'] == LEGACY else 0.,
                        legacy_reference=True)
            item['reason'] = '仅有原冻结小时统计，缺少逐次明细，不能纳入晚补化验或拆分分钟'
    if item.get('legacy_reference'):
        item['reason'] += '；含旧统计参考，旧值未纳入晚补化验'
    item.update(settlement_policy=POLICY, deadline=iso_time(end+DELAY), manual_count=len(basis['records']),
                manual_carried=basis['carried'], recalculated_count=replaced)
    item['reason'] += '；结束后延迟2小时结算'
    if replaced:
        item['reason'] += f'；采用重介化验{basis["value"]:.2f}%（'+('沿用小时前采样' if basis['carried'] else f'{len(basis["records"])}条均值')+'）'
    elif basis['records']:
        item['reason'] += '；本小时未应用晚补化验'
    if not rows and basis['value'] is not None:
        item['reason'] += '；缺少原时段逐次明细，不能回算'
    return dict(policy=POLICY, item=item, heavy_ash=basis, contributions=contributions,
                excluded_reasons=dict(excluded), original_sample_count=len(rows))


def prepare_due_hours(db, now):
    """One current due hour plus one backlog hour per worker tick, bounded read work."""
    end = latest_end(now)
    starts = [end-timedelta(hours=1)]
    older = db.scalar(select(DecisionCycleHour.start).where(DecisionCycleHour.start < iso_time(starts[0]),
        ~DecisionCycleHour.start.in_(select(DecisionAshSettlement.start)))
        .order_by(DecisionCycleHour.start.desc()).limit(1))
    if older:
        starts.append(business_time(older))
    prepared = []
    for start in starts:
        key = iso_time(start)
        if not db.get(DecisionAshSettlement, key):
            prepared.append(DecisionAshSettlement(start=key, end=iso_time(start+timedelta(hours=1)),
                deadline=iso_time(start+timedelta(hours=3)), generated_at=iso_time(now),
                snapshot=build_settlement(db, start)))
    return prepared


def settle_due_hours(db, now):
    db.add_all(prepare_due_hours(db, now))
    db.flush()


def settlement_tick(now, sessions):
    # Do expensive replay in a read snapshot; WAL permits the collector to write.
    with sessions() as db:
        if db.bind.dialect.name == 'sqlite':
            db.execute(text('BEGIN'))
        prepared = prepare_due_hours(db, now)
    if not prepared:
        return
    with sessions() as db:
        if db.bind.dialect.name == 'sqlite':
            db.execute(text('BEGIN IMMEDIATE'))
        for row in prepared:
            if not db.get(DecisionAshSettlement, row.start):
                db.add(row)
        db.commit()


def get_settlement(db, start, now):
    if start+timedelta(hours=1)+DELAY > now:
        raise ValueError('该小时尚未到结束后两小时的结算时间')
    saved = db.get(DecisionAshSettlement, iso_time(start))
    result = deepcopy(saved.snapshot) if saved else build_settlement(db, start)
    result['item']['frozen'] = saved is not None
    return result


def report(items, start, end, now, hourly):
    mass = fsum(i.get('mass_t') or 0 for i in items)
    ashes = fsum(i.get('ash_mass_t') or 0 for i in items)
    legacy_count = sum(i.get('legacy_count', 0) for i in items)
    legacy_sum = fsum(i.get('legacy_sum', 0) for i in items)
    mixed = bool(mass and legacy_count)
    value = None if mixed else 100*ashes/mass if mass else legacy_sum/legacy_count if legacy_count else None
    covered = fsum(i['coverage']*(business_time(i['end'])-business_time(i['start'])).total_seconds() for i in items)
    duration = (end-start).total_seconds()
    valid_count = sum(i['value'] is not None for i in items)
    reference = any(i.get('legacy_reference') for i in items)
    summary = dict(value=value, aggregation='mixed' if mixed else MASS if mass else LEGACY, mass_t=mass,
        legacy_reference=reference,
        sample_count=sum(i['sample_count'] for i in items), expected_count=int(duration/CADENCE),
        coverage=covered/duration, valid_seconds=covered, total_seconds=duration,
        valid_segments=valid_count, total_segments=len(items), valid_hours=valid_count, total_hours=len(items),
        missing_hours=len(items)-valid_count, partial_hours=sum(i['status'] == 'partial' for i in items),
        groups=dict(output_mass=dict(value=100*ashes/mass if mass else None, mass_t=mass),
                    legacy=dict(value=legacy_sum/legacy_count if legacy_count else None, sample_count=legacy_count)))
    return dict(policy='decision-ash-hours-v1' if hourly else 'decision-ash-interval-v1', settlement_policy=POLICY,
        timezone='Asia/Shanghai', unit='%', publication_delay_seconds=int(DELAY.total_seconds()),
        generated_at=iso_time(now), latest_end=iso_time(latest_end(now)),
        period=dict(start=iso_time(start), end=iso_time(end)), items=items, summary=summary,
        method='小时结束后延迟2小时结算；按采样时段纳入截止前重介化验，其他组分保留原记录，按实际产煤量加权；缺失不补零。'+
            ('含旧统计参考，旧值未纳入晚补化验。' if reference else ''))


def read_settled_hours(db, start=None, end=None, now=None):
    now = now or utc_now()
    # Reuse existing range validation with a delayed clock, never alter the original snapshots.
    original = read_ash_hours(db, start, end, now-DELAY)
    start, end = (business_time(original['period'][key]) for key in ('start', 'end'))
    items = [get_settlement(db, business_time(i['start']), now)['item'] for i in original['items']]
    return report(items, start, end, now, True)


def read_settled_interval(db, start=None, end=None, now=None):
    now = now or utc_now()
    latest = latest_end(now)
    if (start is None) != (end is None):
        raise ValueError('请同时选择开始和结束时间')
    if start is None:
        start, end = latest-timedelta(hours=24), latest
    for stamp in (start, end):
        if not 1970 <= stamp.year <= 9998 or stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('时间须包含时区且在支持范围内')
        if stamp.second or stamp.microsecond:
            raise ValueError('最小单位为分钟，请选择整分钟时间')
    if end <= start or end-start > timedelta(days=31):
        raise ValueError('区间须为1分钟至31天')
    if end > latest:
        raise ValueError('小时灰分延迟两小时结算，请选择已结算的时段')
    cursor, items = start, []
    while cursor < end:
        hour = cursor.astimezone(LOCAL).replace(minute=0, second=0, microsecond=0)
        stop = min(end, hour+timedelta(hours=1))
        settled = get_settlement(db, hour, now)
        if cursor == hour and stop == hour+timedelta(hours=1):
            item = settled['item']
        else:
            item = contribution_item(settled['contributions'], cursor, stop)
            item.update(frozen=settled['item']['frozen'], settlement_policy=POLICY, deadline=iso_time(hour+timedelta(hours=3)))
            item['reason'] += '；按该小时结算明细截取分钟区间'
            if settled['item'].get('legacy_reference'):
                item['legacy_reference'] = True
                item['reason'] += '；旧统计参考未纳入晚补化验，无明细的小时不能拆分分钟'
        items.append(item)
        cursor = stop
    return report(items, start, end, now, False)
