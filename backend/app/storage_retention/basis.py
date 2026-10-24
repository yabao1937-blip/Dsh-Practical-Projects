"""Permanent, minimal chemistry evidence from original saved decisions.

These frames support later assays after detailed retention expires. They are not
telemetry records and never run a current model to reconstruct an old prediction.
"""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from math import isfinite
from types import SimpleNamespace
import zlib

from sqlalchemy import select, text

from ..hourly_brief.inputs import business_time
from ..telemetry.domain import iso_time
from .models import RetentionHourBasis

POLICY = 'permanent-hour-chemistry-v1'
MAX_UNPACKED_BYTES = 16 * 1024 * 1024


def _hour(start):
    start = business_time(start) if isinstance(start, str) else start
    if (start.tzinfo is None or start.utcoffset() is None or start.minute
            or start.second or start.microsecond):
        raise ValueError('永久小时依据必须使用含时区的整点')
    return start, start + timedelta(hours=1)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(',', ':')).encode('utf-8')


def _number(value):
    return type(value) in (int, float) and isfinite(value)


def _independent(value):
    if isinstance(value, dict):
        return not {'$reading', '$manual', '$cell'}.intersection(value) and all(
            _independent(child) for child in value.values())
    if isinstance(value, list):
        return all(_independent(child) for child in value)
    return not isinstance(value, float) or isfinite(value)


def _decode(saved):
    try:
        unpacker = zlib.decompressobj()
        raw = unpacker.decompress(saved.payload, MAX_UNPACKED_BYTES + 1)
        if (len(raw) > MAX_UNPACKED_BYTES or not unpacker.eof
                or unpacker.unused_data or unpacker.unconsumed_tail):
            raise ValueError('永久小时依据压缩长度异常')
        if hashlib.sha256(raw).hexdigest() != saved.checksum:
            raise ValueError('永久小时依据校验失败')
        data = json.loads(raw)
        start, end = _hour(saved.start)
        if (data.get('policy') != POLICY or data.get('start') != iso_time(start)
                or data.get('end') != iso_time(end) or saved.end != iso_time(end)):
            raise ValueError('永久小时依据时段或口径不符')
        rows, stopped = data['rows'], data['stopped_intervals']
        if not isinstance(rows, list) or len(rows) > 720 or not isinstance(stopped, list):
            raise ValueError('永久小时依据记录异常')
        previous = None
        for row in rows:
            when = business_time(row['sampled_at'])
            slot, weight = row['slot'], row['weight']
            if (type(slot) is not int or slot != int(when.timestamp()) // 5
                    or not start <= when < end or previous is not None and slot <= previous
                    or not isinstance(row['fingerprint'], str) or not isinstance(row['snapshot'], dict)
                    or not _independent(row['snapshot'])):
                raise ValueError('永久小时依据逐次身份异常')
            previous = slot
            cadence = row['snapshot'].get('cadence_seconds')
            # Historical payloads retain their own duration. A new default must
            # never stretch a five- or thirty-second frame into a minute.
            cadence = int(cadence) if type(cadence) in (int, float) and cadence in (5, 30, 60) else 5
            if (not isinstance(weight, dict)
                    or any(not _number(weight.get(k)) or weight[k] < 0
                           for k in ('seconds', 'duration_seconds', 'mass_t'))
                    or weight['seconds'] > weight['duration_seconds']
                    or weight['duration_seconds'] > min(cadence, (end-when).total_seconds())
                    or weight.get('reason') is not None and not isinstance(weight['reason'], str)):
                raise ValueError('永久小时依据权重异常')
        right = start.timestamp()
        for interval in stopped:
            if (not isinstance(interval, list) or len(interval) != 2
                    or any(not _number(value) for value in interval)
                    or not right <= interval[0] < interval[1] <= end.timestamp()):
                raise ValueError('永久小时依据停机区间异常')
            right = interval[1]
        return data
    except (KeyError, TypeError, AttributeError, OverflowError, zlib.error,
            UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('永久小时依据损坏，保留原始记录') from exc


def prepare_basis(db, start, rows, now):
    """Freeze original chemistry, quality-clipped weights and stopped periods."""
    from ..decision_hourly.ash_settlement import (POLICY as SETTLEMENT_POLICY,
        settlement_snapshot, source_revision)
    from ..decision_hourly.evidence import weights
    from ..decision_hourly.models import DecisionAshSettlement
    from ..decision_hourly.production_coverage import stopped_intervals

    start, end = _hour(start)
    if now.tzinfo is None or now.utcoffset() is None or now < end:
        raise ValueError('只能保存已经结束的完整小时依据')
    rows = sorted(rows, key=lambda row: row.slot)
    weight = weights(rows, end, db=db)
    source = source_revision(db, start)
    saved = db.get(DecisionAshSettlement, iso_time(start))
    prior = saved.snapshot if saved else {}
    if prior.get('policy') == SETTLEMENT_POLICY and prior.get('production_source_revision') == source:
        stopped = deepcopy(prior.get('stopped_intervals', []))
    else:
        choices = {json.dumps(row.snapshot.get('sources', {}).get('resolved_choices', {}),
                              sort_keys=True) for row in rows}
        sources = rows[0].snapshot.get('sources') if rows else None
        stopped = stopped_intervals(db, start, end, sources) if len(choices) <= 1 else []
    frames = []
    for row in rows:
        frame = dict(slot=row.slot, sampled_at=row.sampled_at,
                     fingerprint=row.fingerprint,
                     snapshot=deepcopy(settlement_snapshot(row.snapshot)),
                     weight=deepcopy(weight[row.slot]))
        if hasattr(row, 'context_key'):
            frame['context_key'] = row.context_key
        frames.append(frame)
    raw = _json(dict(policy=POLICY, start=iso_time(start), end=iso_time(end),
                     rows=frames, stopped_intervals=stopped))
    result = dict(start=iso_time(start), end=iso_time(end), created_at=iso_time(now),
                  checksum=hashlib.sha256(raw).hexdigest(), payload=zlib.compress(raw))
    _decode(SimpleNamespace(**result))
    return result


def save_basis(db, prepared):
    """Add once in the caller's short write transaction; never replace evidence."""
    from ..config import REMOTE_READONLY
    if REMOTE_READONLY:
        raise ValueError('只读测试服不能保存永久小时依据')
    _decode(SimpleNamespace(**prepared))
    if db.get_bind().dialect.name == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert
        db.execute(insert(RetentionHourBasis).values(**prepared)
                   .on_conflict_do_nothing(index_elements=['start']))
    elif db.get(RetentionHourBasis, prepared['start']) is None:
        db.add(RetentionHourBasis(**prepared))
        db.flush()
    saved = db.scalar(select(RetentionHourBasis).where(
        RetentionHourBasis.start == prepared['start']).execution_options(populate_existing=True))
    _decode(saved)
    if saved.checksum != prepared['checksum']:
        raise ValueError('永久小时依据已存在不同内容，保留原始记录')
    return saved


def _load(db, start):
    start, _ = _hour(start)
    # A laptop may deploy before the production host. Do not create schema or
    # fail existing read-only chemistry views when that host is still old.
    if (db.get_bind().dialect.name == 'sqlite' and not db.scalar(text(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='retention_hour_bases'"))):
        return None
    saved = db.get(RetentionHourBasis, iso_time(start))
    return _decode(saved) if saved else None


def load_basis_rows(db, start):
    data = _load(db, start)
    if data is None:
        return None
    result = []
    for frame in data['rows']:
        fields = {key: deepcopy(value) for key, value in frame.items() if key != 'weight'}
        row = SimpleNamespace(**fields)
        row._settlement_weight = deepcopy(frame['weight'])
        result.append(row)
    return result


def load_basis_stopped(db, start):
    data = _load(db, start)
    return deepcopy(data['stopped_intervals']) if data is not None else None
