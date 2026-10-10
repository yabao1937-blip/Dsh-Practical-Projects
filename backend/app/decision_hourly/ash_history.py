"""Read frozen hourly ash values; never replay models or write historical hours."""
from datetime import datetime, timedelta
from math import fsum

from sqlalchemy import select

from ..hourly_brief.inputs import LOCAL, numeric
from ..telemetry.domain import iso_time, utc_now
from .models import DecisionCycleHour
from .statistics import EXPECTED
from .evidence import MASS


def read_ash_hours(db, start: datetime | None = None, end: datetime | None = None, now=None):
    now = now or utc_now()
    latest_end = now.astimezone(LOCAL).replace(minute=0, second=0, microsecond=0)
    if (start is None) != (end is None):
        raise ValueError('请同时选择开始和结束时间')
    if start is None:
        start, end = latest_end-timedelta(hours=24), latest_end
    for stamp in (start, end):
        if not 1970 <= stamp.year <= 9998:
            raise ValueError('时间超出支持范围')
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('时间必须包含时区，请使用北京时间')
        local = stamp.astimezone(LOCAL)
        if local.minute or local.second or local.microsecond:
            raise ValueError('只支持整点小时，请选择整点时间')
    start, end = start.astimezone(LOCAL), end.astimezone(LOCAL)
    if end <= start:
        raise ValueError('结束时间必须晚于开始时间')
    if end-start > timedelta(days=31):
        raise ValueError('单次最多查询 31 天，请缩短区间')
    if end > latest_end:
        raise ValueError('只显示已结束的完整小时，结束时间不能晚于当前整点')

    saved = {r.start: r for r in db.scalars(select(DecisionCycleHour).where(
        DecisionCycleHour.start >= iso_time(start), DecisionCycleHour.start < iso_time(end)))}
    items, weighted, counts, masses, ashes = [], [], [], [], []
    legacy_counts = []
    cursor = start
    while cursor < end:
        row = saved.get(iso_time(cursor))
        cell = (row.snapshot.get('total_ash') or {}) if row else {}
        value, count = cell.get('value'), cell.get('sample_count')
        available = (numeric(value) and 0 <= value <= 100 and type(count) is int
                     and 0 < count <= EXPECTED and cell.get('status') == 'average')
        is_mass = cell.get('aggregation') == MASS
        mass, ash_mass = cell.get('mass_t'),cell.get('ash_mass_t')
        seconds = cell.get('valid_seconds')
        if is_mass:
            available = available and numeric(mass) and mass > 0 and numeric(ash_mass) and 0 <= ash_mass <= mass and numeric(seconds) and 0 < seconds <= 3600
        if available:
            counts.append(count)
            if is_mass:
                masses.append(mass)
                ashes.append(ash_mass)
            else:
                weighted.append(value*count)
                legacy_counts.append(count)
        items.append(dict(start=iso_time(cursor), end=iso_time(cursor+timedelta(hours=1)),
            value=value if available else None, sample_count=count if available else 0,
            expected_count=EXPECTED, coverage=count/EXPECTED if available else 0,
            status=('complete' if count == EXPECTED else 'partial') if available else 'missing',
            reason=cell.get('reason') if available else
                (cell.get('reason') or '该小时没有有效总灰分记录' if row else '该小时尚无已保存的统计记录'),
            frozen=row is not None))
        items[-1].update(aggregation=MASS if is_mass else 'legacy-sample-mean',
            mass_t=mass if available and is_mass else None,
            valid_seconds=cell.get('valid_seconds') if is_mass else None)
        if available and is_mass:
            seconds = cell.get('valid_seconds',0)
            items[-1].update(coverage=seconds/3600,
                status='complete' if seconds >= 3595 else 'partial')
        cursor += timedelta(hours=1)
    sample_count = sum(counts)
    mass_value = 100*fsum(ashes)/fsum(masses) if masses else None
    legacy_value = fsum(weighted)/sum(legacy_counts) if legacy_counts else None
    mixed = bool(masses and legacy_counts)
    return dict(policy='decision-ash-hours-v1', timezone='Asia/Shanghai', unit='%',
        generated_at=iso_time(now), latest_end=iso_time(latest_end),
        period=dict(start=iso_time(start), end=iso_time(end)), items=items,
        summary=dict(value=None if mixed else mass_value if masses else legacy_value,
            aggregation='mixed' if mixed else MASS if masses else 'legacy-sample-mean', mass_t=fsum(masses),
            groups=dict(output_mass=dict(value=mass_value,mass_t=fsum(masses)),
                        legacy=dict(value=legacy_value,sample_count=sum(legacy_counts))),
            sample_count=sample_count, expected_count=len(items)*EXPECTED,
            coverage=fsum(item['coverage'] for item in items)/len(items), valid_hours=len(counts),
            total_hours=len(items), missing_hours=len(items)-len(counts),
            partial_hours=sum(item['status'] == 'partial' for item in items)),
        method=('区间跨越新旧统计口径，分别展示均值，不合并成一个数。' if mixed else
            '按实际产煤量加权：Σ(总灰分 × 501+502 流量 × 有效时长) / Σ实际产煤量；缺口不补齐。' if masses else
            '旧口径：先逐次计算总灰分，再取小时有效结果均值；区间按各小时有效样本数加权汇总。缺失不按 0 计入。'))
