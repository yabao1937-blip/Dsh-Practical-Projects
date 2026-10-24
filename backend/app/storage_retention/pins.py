"""Bounded legacy pin backfill; no deletion or global completion claims.

The controller must hold its serialized write transaction while calling this
helper and advancing its durable cursor. Only after every historic hour has
been covered may it release non-NULL last_slot pins below a verified decision
archive watermark. A held value may first occur years before its final use.
"""
from datetime import datetime, timedelta
import hashlib
import json
import zlib

from sqlalchemy import Text, and_, cast, or_, select

from ..decision_hourly.models import DecisionCycleSample, DecisionEvidenceArchive
from ..decision_hourly.references import chunks, pin_reading_bounds, walk
from ..telemetry.domain import iso_time, parse_time
from ..telemetry.models import TelemetryReading


def _stamp(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError('Pin backfill timestamps require timezone.')
        return parse_time(value.isoformat())
    return parse_time(value)


MAX_HOUR_ROWS = 720
MAX_HOUR_BYTES = 64 * 1024 * 1024


def _hot_rows(db, start, end, *, snapshots=False):
    columns = [DecisionCycleSample.slot, DecisionCycleSample.sampled_at,
               DecisionCycleSample.context_key, DecisionCycleSample.fingerprint]
    if snapshots:
        # Preserve original SQL JSON bytes for a cheap equality guard in the
        # short writer phase; no large JSON parsing occurs under its write lock.
        columns.append(cast(DecisionCycleSample.snapshot, Text))
    rows = list(db.execute(select(*columns).where(
        DecisionCycleSample.sampled_at >= start, DecisionCycleSample.sampled_at < end)
        .order_by(DecisionCycleSample.slot).limit(MAX_HOUR_ROWS+1)))
    if len(rows) > MAX_HOUR_ROWS:
        raise ValueError('Pin backfill hour exceeds its row budget.')
    return rows


def _archive_row(db, start):
    return db.execute(select(DecisionEvidenceArchive.checksum,
        DecisionEvidenceArchive.sample_count, DecisionEvidenceArchive.payload).where(
            DecisionEvidenceArchive.start == start)).one_or_none()


def _archive_guard(row):
    return (row.checksum, row.sample_count, hashlib.sha256(row.payload).hexdigest()) if row else None


def prepare_hour_refs(db, start, end):
    """Read and parse one hour without reserving the production writer lock.

    Accepts timezone-aware datetimes or ISO strings. The caller owns commit and
    Existing archives are checksum/count checked and raw reference identities
    verified. The result must be rechecked by commit_hour_refs in the same short
    write transaction as the controller's durable cursor advancement.
    """
    start, end = _stamp(start), _stamp(end)
    if start != start.replace(minute=0, second=0, microsecond=0) or end-start != timedelta(hours=1):
        raise ValueError('Pin backfill requires one complete natural hour.')
    start_key, end_key = iso_time(start), iso_time(end)
    hot = _hot_rows(db, start_key, end_key, snapshots=True)
    if sum(len(row[4].encode('utf-8')) for row in hot) > MAX_HOUR_BYTES:
        raise ValueError('Pin backfill hour exceeds its byte budget.')
    rows = [(row[0], json.loads(row[4])) for row in hot]
    hot_count, archived_count = len(rows), 0
    archive = _archive_row(db, start_key)
    if archive:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(archive.payload, MAX_HOUR_BYTES+1)
        if (len(raw) > MAX_HOUR_BYTES or not decoder.eof
                or decoder.unused_data or decoder.unconsumed_tail):
            raise ValueError('Pin backfill archive exceeds its byte budget.')
        if hashlib.sha256(raw).hexdigest() != archive.checksum:
            raise ValueError('Pin backfill archive checksum mismatch.')
        decoded = json.loads(raw)
        if (not isinstance(decoded, list) or len(decoded) != archive.sample_count
                or len(decoded) > MAX_HOUR_ROWS):
            raise ValueError('Pin backfill archive sample count mismatch.')
        for row in decoded:
            if not isinstance(row, dict) or not isinstance(row.get('snapshot'), dict):
                raise ValueError('Pin backfill archive row is malformed.')
            if not start <= _stamp(row['sampled_at']) < end:
                raise ValueError('Pin backfill archive row falls outside its hour.')
            rows.append((row['slot'], row['snapshot']))
        archived_count = len(decoded)

    bounds = {}
    first_slot, end_slot = int(start.timestamp()//5), int(end.timestamp()//5)
    for slot, snapshot in rows:
        if type(slot) is not int or not first_slot <= slot < end_slot or not isinstance(snapshot, dict):
            raise ValueError('Pin backfill decision row is malformed.')
        for cell in walk(snapshot):
            if '$reading' not in cell:
                continue
            reading_id = cell['$reading']
            if type(reading_id) is not int or reading_id <= 0:
                raise ValueError('Pin backfill reading reference is malformed.')
            lower, upper = bounds.get(reading_id, (slot, slot))
            bounds[reading_id] = min(lower, slot), max(upper, slot)

    # A missing reading is corruption, not proof that a reference can be freed.
    for ids in chunks(bounds):
        found = set(db.scalars(select(TelemetryReading.id).where(TelemetryReading.id.in_(ids))))
        if found != set(ids):
            raise ValueError('Pin backfill authoritative reading is missing.')
    return dict(start=start_key, end=end_key, hot_count=hot_count,
                archived_count=archived_count, reading_count=len(bounds), bounds=bounds,
                guard=dict(hot=[tuple(row[:4]) for row in hot],
                           snapshot_text={row[0]:row[4] for row in hot},
                           archive=_archive_guard(archive)))


def commit_hour_refs(db, prepared):
    """Recheck exact source identities before bounded pin upserts; caller commits."""
    start, end, guard = prepared['start'], prepared['end'], prepared['guard']
    hot = [tuple(row) for row in _hot_rows(db, start, end)]
    if hot != guard['hot'] or _archive_guard(_archive_row(db, start)) != guard['archive']:
        raise ValueError('Pin backfill source changed; retry without advancing cursor.')
    originals = list(guard['snapshot_text'].items())
    for offset in range(0, len(originals), 100):
        group = originals[offset:offset+100]
        matching = set(db.scalars(select(DecisionCycleSample.slot).where(or_(*(
            and_(DecisionCycleSample.slot == slot, cast(DecisionCycleSample.snapshot, Text) == raw)
            for slot, raw in group)))))
        if matching != {slot for slot, _ in group}:
            raise ValueError('Pin backfill snapshot changed; retry without advancing cursor.')
    for ids in chunks(prepared['bounds']):
        found = set(db.scalars(select(TelemetryReading.id).where(TelemetryReading.id.in_(ids))))
        if found != set(ids):
            raise ValueError('Pin backfill authoritative reading is missing.')
    pin_reading_bounds(db, prepared['bounds'])
    return {key:prepared[key] for key in ('start', 'end', 'hot_count', 'archived_count', 'reading_count')}


def backfill_hour_refs(db, start, end):
    """Compatibility helper for callers already owning their transaction."""
    return commit_hour_refs(db, prepare_hour_refs(db, start, end))
