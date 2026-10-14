"""Confirmed zero production removes time, never unknown or carried readings."""
import json
from math import fsum
from sqlalchemy import select
from ..hourly_brief.inputs import COAL_AMOUNT, business_time, numeric
from ..telemetry.domain import iso_time
from ..telemetry.models import TelemetryPoint, TelemetryReading
from ..telemetry.retained import usable_at_receipt
from .statistics import CADENCE

POLICY = 'confirmed-zero-production-v1'


def overlap(left, right, intervals):
    return fsum(max(0., min(right, b)-max(left, a)) for a,b in intervals)


def stopped_intervals(db, start, end, sources=None):
    points = list(db.scalars(select(TelemetryPoint).where(TelemetryPoint.enabled.is_(True))))
    choice = ((sources or {}).get('resolved_choices') or {}).get('coal-rate')
    if choice:
        try:
            source, key = json.loads(choice)
        except (ValueError, TypeError):
            return []
        wash = [p for p in points if (p.source_id, p.key) == (source, key)
                and p.config.get('item_id') == COAL_AMOUNT]
    else:
        wash = [p for p in points if p.config.get('item_id') == COAL_AMOUNT]
    if len(wash) != 1:
        return []
    selected = wash[:]
    for belt in ('501', '502'):
        matches = [p for p in points if p.source_id == wash[0].source_id
            and p.config.get('item_id') == f'TIA_3_4.TIA_3_4.{belt}/COAL_CAP']
        if len(matches) != 1:
            return []
        selected.extend(matches)
    if any(p.config.get('unit') not in ('', None, 't/h') for p in selected):
        return []
    batches = {}
    for p in selected:
        rows = db.scalars(select(TelemetryReading).where(TelemetryReading.source_id == p.source_id,
            TelemetryReading.point_key == p.key, TelemetryReading.received_at >= iso_time(start),
            TelemetryReading.received_at < iso_time(end)).order_by(TelemetryReading.received_at))
        for row in rows:
            group = batches.setdefault(row.received_at, {})
            good = (p.key not in group and usable_at_receipt(row.sample, p.config)
                    and numeric(row.sample.get('value')) and row.sample.get('value') == 0)
            group[p.key] = row.batch_id if good and row.batch_id else None
    times = sorted(batches)
    intervals = []
    for i, stamp in enumerate(times):
        group = batches[stamp]
        if len(group) != 3 or None in group.values() or len(set(group.values())) != 1:
            continue
        left = business_time(stamp).timestamp()
        right = min(left+CADENCE, end.timestamp(),
                    business_time(times[i+1]).timestamp() if i+1 < len(times) else end.timestamp())
        if right > left:
            if intervals and left <= intervals[-1][1]:
                intervals[-1][1] = max(right, intervals[-1][1])
            else:
                intervals.append([left, right])
    return intervals


def fields(start, end, intervals, valid_seconds):
    duration = (end-start).total_seconds()
    stopped = overlap(start.timestamp(), end.timestamp(), intervals)
    expected = max(0., duration-stopped)
    return dict(coverage_policy=POLICY, total_seconds=duration, stopped_seconds=stopped,
        expected_production_seconds=expected, valid_seconds=valid_seconds,
        coverage=min(1., valid_seconds/expected) if expected else 0.,
        wall_coverage=min(1., valid_seconds/duration) if duration else 0.)
