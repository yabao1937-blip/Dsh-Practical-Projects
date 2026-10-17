"""Batch the read-only hourly table without loading replay contributions.

Production settlement and explicit historical replay keep their original reader.
Without an injected reader, stale or absent settlements are pending. Only the
test-server producer can inject its bounded in-memory replay for the last ten
hours; neither path writes a settlement to the host.
"""
from copy import deepcopy
from datetime import timedelta

from sqlalchemy import and_, case, func, or_, select

from ..decision_hourly.ash_history import hour_range
from ..decision_hourly.ash_settlement import (
    DELAY, POLICY, available_assays, contribution_item, manual_basis, report, revision,
)
from ..decision_hourly.models import (
    DecisionAshSettlement, DecisionCycleHour, DecisionCycleSample, DecisionEvidenceArchive,
)
from ..telemetry.domain import iso_time, utc_now


def _sources(db, starts):
    keys = [iso_time(start) for start in starts]
    completed = dict(db.execute(select(DecisionCycleHour.start, DecisionCycleHour.generated_at)
        .where(DecisionCycleHour.start.in_(keys))).all())
    # Match source_revision's truthy check, including an existing empty timestamp.
    missing = [start for start in starts if not completed.get(iso_time(start))]
    sources = {key: value for key, value in completed.items() if value}
    if not missing:
        return sources

    bounds = [(and_(DecisionCycleSample.sampled_at >= iso_time(start),
                    DecisionCycleSample.sampled_at < iso_time(start+timedelta(hours=1))),
               iso_time(start)) for start in missing]
    bucket = case(*bounds)
    hot = {key: [count, slot] for key, count, slot in db.execute(
        select(bucket, func.count(), func.max(DecisionCycleSample.slot))
        .where(or_(*(condition for condition, _key in bounds))).group_by(bucket))}
    missing_keys = [iso_time(start) for start in missing]
    archives = dict(db.execute(select(DecisionEvidenceArchive.start, DecisionEvidenceArchive.checksum)
        .where(DecisionEvidenceArchive.start.in_(missing_keys))).all())
    sources.update({key: [*hot.get(key, [0, None]), archives.get(key)] for key in missing_keys})
    return sources


def read_settled_hours(db, start=None, end=None, now=None, *, settlement_reader=None):
    """Return the existing 24-hour JSON using at most five bounded SELECTs."""
    now = now or utc_now()
    start, end = hour_range(start, end, now-DELAY)
    starts, cursor = [], start
    while cursor < end:
        starts.append(cursor)
        cursor += timedelta(hours=1)

    available = available_assays(db, now)
    sources = _sources(db, starts)
    # JSON projection deliberately excludes potentially large contributions and
    # all archived evidence payloads. Only the public item and cache guard cross
    # the read-only transport.
    saved = {key: (guard, item) for key, guard, item in db.execute(select(
        DecisionAshSettlement.start,
        DecisionAshSettlement.snapshot['revision'].as_string(),
        DecisionAshSettlement.snapshot['item'])
        .where(DecisionAshSettlement.start.in_([iso_time(stamp) for stamp in starts])))}
    items = []
    for stamp in starts:
        stop = stamp+timedelta(hours=1)
        basis = manual_basis(db, stamp, stop, now, available)
        guard, cached_item = saved.get(iso_time(stamp), (None, None))
        if guard == revision(basis, sources[iso_time(stamp)]):
            item = deepcopy(cached_item)
            item.update(frozen=False, cached=True)
        elif settlement_reader is not None and stamp >= end-timedelta(hours=10):
            item = settlement_reader(db, stamp, now, basis=basis,
                                     source=sources[iso_time(stamp)])['item']
        else:
            item = contribution_item([], stamp, stop)
            item.update(status='pending', pending=True, frozen=False, cached=False,
                settlement_policy=POLICY, publication_at=iso_time(stop+DELAY),
                deadline=iso_time(stop+DELAY), provisional=basis['carried'],
                reason='小时灰分后台结算中，请稍候')
        items.append(item)
    return report(items, start, end, now, True)
