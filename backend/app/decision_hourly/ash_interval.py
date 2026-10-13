"""Minute-bounded reads of saved predictions; no model replay or history writes."""
from datetime import datetime, timedelta
from math import fsum
from types import SimpleNamespace

from sqlalchemy import select, func

from ..hourly_brief.inputs import LOCAL
from ..telemetry.domain import iso_time, utc_now
from .archive import archived_rows
from .ash_history import read_ash_hours
from .evidence import POLICY, MASS, at, weights, mass_cell
from .models import DecisionCycleSample, DecisionEvidenceArchive
from .references import hydrate_rows
from .statistics import CADENCE, valid

LEGACY = 'legacy-sample-mean'


def partial_item(db, start, end, has_rows=True):
    hour = start.replace(minute=0, second=0, microsecond=0)
    hour_end = hour+timedelta(hours=1)
    # Load the whole hour so an earlier occurrence of a repeated batch still wins.
    # Complete hours use frozen summaries when available; missing summaries can use detail.
    rows = {r['slot']:SimpleNamespace(**r) for r in archived_rows(db,iso_time(hour))} if has_rows else {}
    hot = list(db.scalars(select(DecisionCycleSample).where(
        DecisionCycleSample.sampled_at >= iso_time(hour),
        DecisionCycleSample.sampled_at < iso_time(hour_end)).order_by(DecisionCycleSample.slot))) if has_rows else []
    rows.update({r.slot:r for r in hydrate_rows(db,hot)})
    rows = sorted(rows.values(),key=lambda r:r.slot)
    w = weights(rows,hour_end)
    selected, legacy, seen = [], [], set()
    for r in rows:
        when = at(r)
        if when >= end or when+timedelta(seconds=CADENCE) <= start:
            continue
        if r.snapshot.get('evidence_policy') != POLICY:
            c = r.snapshot.get('total_ash',{})
            if start <= when < end and r.fingerprint not in seen and valid(c) and 0 <= c['value'] <= 100:
                legacy.append(c['value'])
                seen.add(r.fingerprint)
            continue
        weight = w[r.slot]
        seconds = weight['seconds']
        clipped = max(0.,(min(end,when+timedelta(seconds=seconds))-max(start,when)).total_seconds())
        w[r.slot] = dict(weight,seconds=clipped,mass_t=weight['mass_t']*clipped/seconds if seconds else 0.)
        selected.append(r)
    cell = mass_cell(selected,w)
    mass, ashes = cell['mass_t'],cell['ash_mass_t']
    mixed = bool(mass and legacy)
    count = cell['sample_count']+len(legacy)
    seconds = (end-start).total_seconds()
    valid_seconds = min(seconds,cell['valid_seconds']+len(legacy)*CADENCE)
    value = None if mixed else cell['value'] if mass else fsum(legacy)/len(legacy) if legacy else None
    reason = ('区间含产煤量加权与旧样本均值，不能合并' if mixed else cell['reason'] if mass else
              '旧口径：区间内已保存逐次灰分的样本均值' if legacy else
              '此时段无有效逐次灰分与产量配对记录' if rows else '此时段无逐次明细，不能从小时均值推算分钟灰分')
    return dict(start=iso_time(start),end=iso_time(end),value=value,sample_count=count,
        expected_count=int(seconds/CADENCE),coverage=valid_seconds/seconds,
        status='missing' if value is None else 'complete' if valid_seconds>=seconds-1e-6 else 'partial',
        reason=reason,frozen=False,aggregation='mixed' if mixed else MASS if mass or not legacy else LEGACY,
        mass_t=mass,ash_mass_t=ashes,valid_seconds=valid_seconds,
        legacy_sum=fsum(legacy),legacy_count=len(legacy))


def read_ash_interval(db, start: datetime | None = None, end: datetime | None = None, now=None):
    now = now or utc_now()
    latest = now.astimezone(LOCAL).replace(second=0,microsecond=0)
    if (start is None) != (end is None):
        raise ValueError('请同时选择开始和结束时间')
    if start is None:
        start,end = latest-timedelta(hours=24),latest
    for stamp in (start,end):
        if not 1970 <= stamp.year <= 9998:
            raise ValueError('时间超出支持范围')
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('时间必须包含时区，请使用北京时间')
        local = stamp.astimezone(LOCAL)
        if local.second or local.microsecond:
            raise ValueError('最小单位为分钟，请选择整分钟时间')
    start,end = start.astimezone(LOCAL),end.astimezone(LOCAL)
    if end <= start:
        raise ValueError('结束时间必须晚于开始时间，最短 1 分钟')
    if end-start > timedelta(days=31):
        raise ValueError('单次最多查询 31 天，请缩短区间')
    if end > latest:
        raise ValueError('结束时间不能晚于当前分钟，只统计已结束的分钟')
    full_start = start.replace(minute=0)+(timedelta(hours=1) if start.minute else timedelta())
    full_end = end.replace(minute=0)
    frozen = {i['start']:i for i in read_ash_hours(db,full_start,full_end,now)['items']} if full_start<full_end else {}
    # One covering-index scan avoids opening each of up to 745 empty hours.
    first_hour = iso_time(start.replace(minute=0))
    hour_prefix = func.substr(DecisionCycleSample.sampled_at,1,13)
    detail_hours = set(db.scalars(select(hour_prefix).where(DecisionCycleSample.sampled_at>=first_hour,
        DecisionCycleSample.sampled_at<iso_time(end)).distinct()))
    detail_hours.update(s[:13] for s in db.scalars(select(DecisionEvidenceArchive.start).where(
        DecisionEvidenceArchive.start>=first_hour,DecisionEvidenceArchive.start<iso_time(end))))
    items, cursor = [], start
    while cursor < end:
        stop = min(end,cursor.replace(minute=0)+timedelta(hours=1))
        item = frozen.get(iso_time(cursor))
        if item is None or not item['frozen']:
            item = partial_item(db,cursor,stop,iso_time(cursor)[:13] in detail_hours)
        else:
            item = dict(item,legacy_count=item['sample_count'] if item['aggregation']==LEGACY else 0,
                        legacy_sum=item['value']*item['sample_count'] if item['aggregation']==LEGACY and item['value'] is not None else 0.)
        items.append(item)
        cursor = stop
    mass = fsum(i.get('mass_t') or 0 for i in items)
    ashes = fsum(i.get('ash_mass_t') or 0 for i in items)
    legacy_count = sum(i['legacy_count'] for i in items)
    legacy_sum = fsum(i['legacy_sum'] for i in items)
    mixed = bool(mass and legacy_count)
    duration = (end-start).total_seconds()
    covered = fsum(i['coverage']*(datetime.fromisoformat(i['end'])-datetime.fromisoformat(i['start'])).total_seconds() for i in items)
    value = None if mixed else 100*ashes/mass if mass else legacy_sum/legacy_count if legacy_count else None
    return dict(policy='decision-ash-interval-v1',timezone='Asia/Shanghai',unit='%',
        generated_at=iso_time(now),latest_end=iso_time(latest),period=dict(start=iso_time(start),end=iso_time(end)),items=items,
        summary=dict(value=value,aggregation='mixed' if mixed else MASS if mass else LEGACY,mass_t=mass,
            sample_count=sum(i['sample_count'] for i in items),expected_count=int(duration/CADENCE),
            coverage=covered/duration,valid_seconds=covered,total_seconds=duration,
            valid_segments=sum(i['value'] is not None for i in items),total_segments=len(items),
            groups=dict(output_mass=dict(value=100*ashes/mass if mass else None,mass_t=mass),
                        legacy=dict(value=legacy_sum/legacy_count if legacy_count else None,sample_count=legacy_count))),
        method='按所选分钟区间内已保存的预测灰分和实际产煤量加权；开始时间包含，结束时间不包含，缺失不补零。')
