"""Durable hour-first setpoint basis, captured by the serialized decision worker.

GETs only read this evidence. Target and lab revisions never move the anchor;
source/model/parameter conflicts remain visible to the hourly policy.
"""
from copy import deepcopy
from datetime import timedelta

from ..decision_hourly.models import GptHourlyDensityAnchor
from ..hourly_brief.inputs import LOCAL, business_time, numeric
from ..telemetry.domain import iso_time

ORIGIN = 'hour-first-valid-md-set'


def hour_start(now):
    return now.astimezone(LOCAL).replace(minute=0, second=0, microsecond=0)


def capture(db, now, current, definition):
    """Caller owns BEGIN IMMEDIATE and commits anchor with the decision sample."""
    key = iso_time(hour_start(now))
    existing = db.get(GptHourlyDensityAnchor, key)
    if existing:
        return existing
    basis = current.get('execution_basis') or {}
    parts = basis.get('components') or {}
    if basis.get('status') != 'good' or not numeric(basis.get('value')) or not parts:
        return None
    for part in parts.values():
        try:
            age = (now-business_time(part['observed_at'])).total_seconds()
        except (KeyError, TypeError, ValueError):
            return None
        if (part.get('status') != 'good' or not numeric(part.get('value')) or not 1 < part['value'] < 3
                or not 0 <= age <= 30 or part.get('unit') != 'g/cm³'):
            return None
    row = GptHourlyDensityAnchor(start=key, captured_at=iso_time(now),
        snapshot=dict(basis=deepcopy(basis), context=deepcopy(definition), basis_origin=ORIGIN))
    db.add(row)
    db.flush()
    return row


def read(db, now):
    start = hour_start(now)
    row = db.get(GptHourlyDensityAnchor, iso_time(start))
    return dict(start=iso_time(start), end=iso_time(start+timedelta(hours=1)),
        label=f'{start:%m-%d %H:%M}–{start+timedelta(hours=1):%H:%M}',
        value=row.snapshot['basis']['value'] if row else None, unit='g/cm³',
        status='frozen' if row else 'missing', captured_at=row.captured_at if row else None,
        basis_origin=ORIGIN, reason='本小时首次有效设定密度，补录和目标调整仍使用同一基准' if row else
            '本小时尚未采到有效的同源同路同批设定密度，等待后台采集',
        execution_basis=deepcopy(row.snapshot['basis']) if row else None,
        context=deepcopy(row.snapshot['context']) if row else None)
