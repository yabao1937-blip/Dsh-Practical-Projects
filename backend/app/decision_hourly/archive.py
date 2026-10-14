"""Lossless compressed evidence before removing old hot rows; hour summaries stay frozen."""
import hashlib
import json
import zlib
from sqlalchemy import select, delete, text
from .models import DecisionCycleSample, DecisionCycleHour, DecisionEvidenceArchive
from .references import resolve_snapshots


def prepare_archive(db, cutoff):
    hour = db.scalar(select(DecisionCycleHour).where(DecisionCycleHour.end <= cutoff,
        DecisionCycleHour.start.in_(select(DecisionCycleHour.start).join(DecisionCycleSample,
            (DecisionCycleSample.sampled_at >= DecisionCycleHour.start) &
            (DecisionCycleSample.sampled_at < DecisionCycleHour.end))))
        .order_by(DecisionCycleHour.start).limit(1))
    if not hour:
        return
    rows = list(db.scalars(select(DecisionCycleSample).where(DecisionCycleSample.sampled_at >= hour.start,
        DecisionCycleSample.sampled_at < hour.end).order_by(DecisionCycleSample.slot)))
    data = [dict(slot=r.slot, sampled_at=r.sampled_at, context_key=r.context_key,
                 fingerprint=r.fingerprint, snapshot=r.snapshot) for r in rows]
    raw = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',',':')).encode()
    checksum = hashlib.sha256(raw).hexdigest()
    saved = db.get(DecisionEvidenceArchive,hour.start)
    if saved:
        if saved.checksum != checksum or hashlib.sha256(zlib.decompress(saved.payload)).hexdigest() != checksum:
            raise ValueError('逐次证据归档校验不一致，保留原始记录')
    else:
        payload = zlib.compress(raw)
        if zlib.decompress(payload) != raw:
            raise ValueError('逐次证据归档校验失败')
    return dict(start=hour.start,end=hour.end,checksum=checksum,
                payload=saved.payload if saved else payload,
                signatures=[(r.slot,r.sampled_at,r.context_key,r.fingerprint) for r in rows])


def commit_archive(db, prepared):
    if not prepared:
        return
    saved = db.get(DecisionEvidenceArchive,prepared['start'])
    if saved and (saved.checksum != prepared['checksum'] or saved.payload != prepared['payload']):
        raise ValueError('逐次证据归档校验不一致，保留原始记录')
    # Decisions are append-only. Recheck their identities before deleting exactly
    # the rows encoded in the verified payload; never delete a concurrently added row.
    signatures = [tuple(row) for row in db.execute(select(DecisionCycleSample.slot,
        DecisionCycleSample.sampled_at,DecisionCycleSample.context_key,DecisionCycleSample.fingerprint)
        .where(DecisionCycleSample.sampled_at >= prepared['start'],DecisionCycleSample.sampled_at < prepared['end'])
        .order_by(DecisionCycleSample.slot))]
    if signatures != prepared['signatures']:
        return
    if not saved:
        db.add(DecisionEvidenceArchive(start=prepared['start'],payload=prepared['payload'],
            checksum=prepared['checksum'],sample_count=len(signatures)))
        db.flush()
    db.execute(delete(DecisionCycleSample).where(DecisionCycleSample.slot.in_([row[0] for row in signatures])))


def archive_old_hour(db, cutoff):
    """Atomic helper for callers already owning a transaction."""
    commit_archive(db,prepare_archive(db,cutoff))


def archive_tick(cutoff, sessions):
    with sessions() as db:
        if db.bind.dialect.name == 'sqlite':
            db.execute(text('BEGIN'))
        prepared = prepare_archive(db,cutoff)
    if not prepared:
        return
    with sessions() as db:
        if db.bind.dialect.name == 'sqlite':
            db.execute(text('BEGIN IMMEDIATE'))
        commit_archive(db,prepared)
        db.commit()


def read_sample(db, slot):
    row = db.get(DecisionCycleSample,slot)
    if row:
        return dict(slot=row.slot,sampled_at=row.sampled_at,context_key=row.context_key,
                    snapshot=resolve_snapshots(db,[row.snapshot])[0])
    from datetime import datetime, timezone
    from ..telemetry.domain import iso_time
    hour = iso_time(datetime.fromtimestamp(slot*5,timezone.utc).replace(minute=0,second=0,microsecond=0))
    saved = db.get(DecisionEvidenceArchive,hour)
    if not saved:
        return None
    raw = zlib.decompress(saved.payload)
    if hashlib.sha256(raw).hexdigest() != saved.checksum:
        raise ValueError('逐次证据归档校验失败')
    found = next((r for r in json.loads(raw) if r['slot']==slot),None)
    if found:
        found['snapshot'] = resolve_snapshots(db,[found['snapshot']])[0]
    return found


def archived_rows(db, start):
    saved=db.get(DecisionEvidenceArchive,start)
    if not saved:
        return []
    raw=zlib.decompress(saved.payload)
    if hashlib.sha256(raw).hexdigest()!=saved.checksum:
        raise ValueError('逐次证据归档校验失败')
    rows = json.loads(raw)
    snapshots = resolve_snapshots(db,[r['snapshot'] for r in rows])
    return [dict(r,snapshot=s) for r,s in zip(rows,snapshots)]
