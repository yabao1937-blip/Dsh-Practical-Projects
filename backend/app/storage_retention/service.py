"""One bounded maintenance unit; never scans or VACUUMs the full database.

Publication and cleanup are two phases. A cold bundle proves isolated restore
before the first live DELETE. Subsequent raw deletes require exact row receipts
and a complete maximum-reference-slot backfill, not first-use pin timestamps.
"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import zlib

from sqlalchemy import delete, select, text

from ..decision_hourly.models import (DecisionCycleContext, DecisionCycleHour,
    DecisionCycleSample, DecisionEvidenceArchive, DecisionTelemetryPin)
from ..decision_hourly.references import hydrate_rows, resolve_snapshots, walk, chunks
from ..models import ManualMeasurement
from ..telemetry.domain import iso_time, utc_now
from ..telemetry.models import TelemetryReading
from .bundle import ArchiveBundleWriter, verify_bundle
from .locking import maintenance_lock
from .models import RetentionArchive, RetentionState
from .policy import RetentionPolicy, assert_writable, storage_root

STATE = 'maintenance-v1'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()


def _hour(value):
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('Retention timestamps must include a timezone')
    return stamp.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _state(db):
    row = db.get(RetentionState, STATE)
    return dict(row.value) if row else {'database_id': str(uuid4()), 'pin_scan_complete': False}


def _put_state(db, value):
    row = db.get(RetentionState, STATE)
    if row:
        row.value = dict(value)
    else:
        db.add(RetentionState(key=STATE, value=dict(value)))


def _begin(db):
    if db.bind.dialect.name == 'sqlite':
        db.execute(text('BEGIN IMMEDIATE'))


def _next_hour(db, after=None, before=None, *, raw=True):
    candidates = []
    for model, column in ((DecisionCycleSample, DecisionCycleSample.sampled_at),
                           (DecisionEvidenceArchive, DecisionEvidenceArchive.start),
                           *(([(TelemetryReading, TelemetryReading.received_at)]) if raw else [])):
        query = select(column)
        if after:
            query = query.where(column >= after)
        if before:
            query = query.where(column < before)
        value = db.scalar(query.order_by(column).limit(1))
        if value:
            candidates.append(_hour(value))
    return min(candidates) if candidates else None


def _pin_scan(sessions, state, now):
    from .pins import prepare_hour_refs, commit_hour_refs
    # Read a small candidate first; parsing one old hour is the largest unit.
    with sessions() as db:
        start = _next_hour(db, state.get('pin_scan_cursor'), raw=False)
        prepared = prepare_hour_refs(db, start, start+timedelta(hours=1)) if start is not None else None
    with sessions() as db:
        _begin(db)
        if start is None:
            state['pin_scan_complete'] = True
            result = {'phase': 'pin-scan-complete'}
        else:
            end = start+timedelta(hours=1)
            result = commit_hour_refs(db, prepared)
            state['pin_scan_cursor'] = iso_time(end)
            result['phase'] = 'pin-scan'
        state['last_result'] = result
        state['checked_at'] = iso_time(now)
        _put_state(db, state)
        db.commit()
    return result


def _plain_row(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _raw_signature(row):
    return sha256(canonical(_plain_row(row))).hexdigest()


def _decisions(db, start, end, limit):
    archived = db.get(DecisionEvidenceArchive, iso_time(start))
    packed = {}
    archive_signature = None
    if archived:
        raw = zlib.decompress(archived.payload)
        if sha256(raw).hexdigest() != archived.checksum:
            raise ValueError('Existing decision archive checksum failed; nothing deleted')
        from ..telemetry.metadata import unpack
        data = unpack(db, json.loads(raw))
        if len(data) != archived.sample_count or len(data) > limit:
            raise ValueError('Existing decision archive row count is invalid')
        packed = {r['slot']: r for r in data}
        if len(packed) != len(data):
            raise ValueError('Duplicate decision slots')
        archive_signature = archived.checksum
    hot = list(db.scalars(select(DecisionCycleSample).where(
        DecisionCycleSample.sampled_at >= iso_time(start), DecisionCycleSample.sampled_at < iso_time(end))
        .order_by(DecisionCycleSample.slot).limit(limit+1)))
    if len(hot) > limit:
        raise ValueError('Hour exceeds maintenance row budget')
    packed.update({r.slot: _plain_row(r) for r in hot})
    if any(not start <= datetime.fromisoformat(r['sampled_at']) < end for r in packed.values()):
        raise ValueError('Decision archive contains out-of-hour evidence')
    rows = [SimpleNamespace(**r) for r in sorted(packed.values(), key=lambda r:r['slot'])]
    resolved = hydrate_rows(db, rows)
    return rows, resolved, archive_signature, [(r.slot, r.fingerprint, sha256(canonical(r.snapshot)).hexdigest()) for r in hot]


def _prepare(sessions, start, now, policy):
    from .basis import prepare_basis
    end = start+timedelta(hours=1)
    with sessions() as db:
        frozen = db.get(DecisionCycleHour, iso_time(start))
        packed, rows, old_archive, hot_signatures = _decisions(db, start, end, policy.max_unit_rows)
        if rows and not frozen:
            raise ValueError('Original hourly summary is missing; preserving detailed evidence')
        raw_rows = list(db.scalars(select(TelemetryReading).where(
            TelemetryReading.received_at >= iso_time(start), TelemetryReading.received_at < iso_time(end))
            .order_by(TelemetryReading.received_at, TelemetryReading.id).limit(policy.max_unit_rows+1)))
        if len(raw_rows) + len(rows) > policy.max_unit_rows:
            raise ValueError('Hour exceeds maintenance row budget')
        # Original raw references can lie before the hour (90s level lag). Include
        # these dependencies as well, while deleting only the hour's own readings.
        ids, manual = set(), set()
        for row in packed:
            for value in walk(row.snapshot):
                if '$reading' in value:
                    ids.add(value['$reading'])
                if '$manual' in value:
                    manual.add(value['$manual'])
        boundary_rows = list(db.scalars(select(TelemetryReading).where(
            TelemetryReading.received_at >= iso_time(start-timedelta(seconds=90)),
            TelemetryReading.received_at < iso_time(start)).limit(policy.max_unit_rows+1)))
        raw_by_id = {r.id:r for r in [*raw_rows,*boundary_rows]}
        for group in chunks(ids-raw_by_id.keys()):
            raw_by_id.update({r.id:r for r in db.scalars(select(TelemetryReading).where(TelemetryReading.id.in_(group)))})
        if ids-raw_by_id.keys():
            raise ValueError('Cold archive reference closure is incomplete')
        contexts = [db.get(DecisionCycleContext, k) for k in sorted({r.context_key for r in rows})]
        if any(r is None for r in contexts):
            raise ValueError('Original model/context definition is missing')
        manuals = []
        for group in chunks(manual):
            manuals.extend(db.scalars(select(ManualMeasurement).where(ManualMeasurement.id.in_(group))))
        if {r.id for r in manuals} != manual:
            raise ValueError('Original manual evidence is missing')
        basis = prepare_basis(db, start, rows, now) if rows else None
        # All SQL reads finish before compression/restore verification begins.
        tables = {
            'telemetry_readings': [_plain_row(r) for r in raw_by_id.values()],
            'decision_original_packed': [dict(slot=r.slot, sampled_at=r.sampled_at, context_key=r.context_key,
                fingerprint=r.fingerprint, snapshot=r.snapshot) for r in packed],
            'decision_resolved': [dict(slot=r.slot, sampled_at=r.sampled_at, context_key=r.context_key,
                fingerprint=r.fingerprint, snapshot=r.snapshot) for r in rows],
            'decision_cycle_contexts': [_plain_row(r) for r in contexts],
            'manual_measurements': [_plain_row(r) for r in manuals],
            'decision_cycle_hours': [_plain_row(frozen)] if frozen else [],
            'retention_hour_bases': [basis] if basis else [],
        }
        receipts = {str(r.id):_raw_signature(r) for r in raw_rows}
        guard = {'old_archive': old_archive, 'hot_signatures':hot_signatures,
                 'hour':sha256(canonical(_plain_row(frozen))).hexdigest() if frozen else None}
    return dict(start=iso_time(start), end=iso_time(end), tables=tables, basis=basis,
                receipts=receipts, guard=guard, sample_count=len(rows))


def _encoded_table(rows):
    if not rows:
        return None
    columns = list(rows[0])
    values = [[json.dumps(row[c], ensure_ascii=False, allow_nan=False, separators=(',',':'))
        if isinstance(row[c], (dict, list)) else row[c] for c in columns] for row in rows]
    return columns, values


def _manifest(root, row):
    path = root / row.manifest
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError('Cold archive path is outside production storage')
    current = root
    for part in path.relative_to(root).parts:
        current = current/part
        if current.exists() and (current.is_symlink() or getattr(current.lstat(),'st_file_attributes',0) & 0x400):
            raise ValueError('Cold archive path contains a link')
    if sha256(path.read_bytes()).hexdigest() != row.checksum:
        raise ValueError('Cold archive manifest changed; source data retained')
    return path


def _verify_members(root, row):
    """Recheck compressed members cheaply; isolated restore already passed at publish."""
    path = _manifest(root, row)
    manifest = json.loads(path.read_bytes())
    for member in manifest['tables']:
        file = path.parent / member['path']
        if file.is_symlink() or not file.resolve().is_relative_to(path.parent.resolve()):
            raise ValueError('Archive member path is unsafe')
        digest, size = sha256(), 0
        with file.open('rb') as stream:
            while block := stream.read(1024*1024):
                digest.update(block)
                size += len(block)
        if size != member['compressed_bytes'] or digest.hexdigest() != member['compressed_sha256']:
            raise ValueError('Archive member checksum failed; source rows retained')
    return manifest


def _publish(sessions, root, prepared, state, now, policy):
    from .basis import save_basis
    identity = sha256(canonical([prepared['start'], prepared['guard'], prepared['receipts']])).hexdigest()[:16]
    archive_id = _hour(prepared['start']).strftime('%Y%m%dT%H')+'-'+identity
    month = root / 'archives' / _hour(prepared['start']).strftime('%Y-%m')
    path = month / archive_id / 'manifest.json'
    if path.exists():
        verified = verify_bundle(path)
        if verified['metadata'].get('database_id') != state['database_id']:
            raise ValueError('Existing archive belongs to a different database')
    else:
        writer = ArchiveBundleWriter(month, archive_id,
            {'database_id':state['database_id'], 'start':prepared['start'], 'end':prepared['end'],
             'policy':'30-day-online-365-day-detail-v1', 'guard':prepared['guard']},
            max_uncompressed_bytes=policy.max_unit_bytes, max_rows=policy.max_unit_rows*4,
            budget_seconds=30)
        try:
            for name, rows in prepared['tables'].items():
                encoded = _encoded_table(rows)
                if encoded:
                    writer.add_rows(name, *encoded)
            path = writer.finish()
        finally:
            writer.abort()
    checksum = sha256(path.read_bytes()).hexdigest()
    point_ranges = {}
    for raw in prepared['tables']['telemetry_readings']:
        point = sha256((raw['source_id']+'\0'+raw['point_key']).encode()).hexdigest()
        bounds = point_ranges.setdefault(point,[raw['id'],raw['id']])
        bounds[0], bounds[1] = min(bounds[0],raw['id']), max(bounds[1],raw['id'])
    start, end = _hour(prepared['start']), _hour(prepared['end'])
    with sessions() as db:
        _begin(db)
        old_archive = db.scalar(select(DecisionEvidenceArchive.checksum).where(DecisionEvidenceArchive.start == prepared['start']))
        hot = db.scalars(select(DecisionCycleSample).where(DecisionCycleSample.sampled_at >= prepared['start'],
            DecisionCycleSample.sampled_at < prepared['end']).order_by(DecisionCycleSample.slot))
        signatures = [(r.slot, r.fingerprint, sha256(canonical(r.snapshot)).hexdigest()) for r in hot]
        frozen = db.get(DecisionCycleHour, prepared['start'])
        actual = {'old_archive':old_archive, 'hot_signatures':signatures,
            'hour':sha256(canonical(_plain_row(frozen))).hexdigest() if frozen else None}
        if actual != prepared['guard']:
            raise ValueError('Hour changed during archive preparation; source data retained')
        raw_ids = set(db.scalars(select(TelemetryReading.id).where(TelemetryReading.received_at >= prepared['start'],
            TelemetryReading.received_at < prepared['end'])))
        if raw_ids != {int(k) for k in prepared['receipts']}:
            raise ValueError('Raw evidence arrived during archival; source data retained')
        if db.get(RetentionArchive, prepared['start']):
            return {'phase':'already-published', 'start':prepared['start']}
        if prepared['basis']:
            save_basis(db, prepared['basis'])
        db.add(RetentionArchive(start=prepared['start'], end=prepared['end'], archive_id=archive_id,
            manifest=path.relative_to(root).as_posix(), checksum=checksum, created_at=iso_time(now),
            status='deleting', sample_count=prepared['sample_count'], raw_count=len(prepared['receipts']),
            raw_min_id=min((r['id'] for r in prepared['tables']['telemetry_readings']),default=None),
            raw_max_id=max((r['id'] for r in prepared['tables']['telemetry_readings']),default=None),
            point_ranges=point_ranges,
            receipts=zlib.compress(canonical(prepared['receipts']))))
        db.execute(delete(DecisionCycleSample).where(DecisionCycleSample.sampled_at >= prepared['start'],
            DecisionCycleSample.sampled_at < prepared['end']))
        db.execute(delete(DecisionEvidenceArchive).where(DecisionEvidenceArchive.start == prepared['start']))
        state['archive_cursor'] = prepared['end']
        state['last_result'] = {'phase':'archived', 'start':prepared['start'], 'samples':prepared['sample_count']}
        state['checked_at'] = iso_time(now)
        _put_state(db, state)
        db.commit()
    return state['last_result']


def _delete_raw_batch(sessions, root, state, policy):
    if not state.get('pin_scan_complete') or not state.get('archive_cursor'):
        return None
    with sessions() as db:
        query = select(RetentionArchive).where(RetentionArchive.status.in_(('deleting','retained-pins')))
        candidate = db.scalar(query.where(RetentionArchive.start > state.get('cleanup_cursor',''))
            .order_by(RetentionArchive.start).limit(1))
        if candidate is None:
            candidate = db.scalar(query.order_by(RetentionArchive.start).limit(1))
        if not candidate:
            return None
        _manifest(root, candidate)
        manifest = _verify_members(root, candidate)
        if manifest['metadata'].get('database_id') != state['database_id']:
            raise ValueError('Cold archive database identity mismatch')
        start = candidate.start
        receipt_copy = candidate.receipts
        receipts = json.loads(zlib.decompress(receipt_copy)) if receipt_copy else {}
        # Read only a bounded candidate batch before reserving the writer lock.
        # Keep the highest raw id permanently to prevent SQLite INTEGER PK reuse.
        maximum = db.scalar(select(TelemetryReading.id).order_by(TelemetryReading.id.desc()).limit(1))
        proposed = []
        cutoff_slot = int(datetime.fromisoformat(state['archive_cursor']).timestamp())//5
        # Quality-event rows can truncate the NEXT hour's delayed level even
        # when no snapshot contains a $reading reference to that bad event.
        # Keep the full 90s boundary until that next hour's basis is frozen and
        # archive_cursor has advanced past it. Pins alone cannot protect this.
        boundary_cutoff = datetime.fromisoformat(state['archive_cursor'])-timedelta(seconds=90)
        present_ids = set()
        for group in chunks([int(k) for k in receipts]):
            present_ids.update(db.scalars(select(TelemetryReading.id).where(TelemetryReading.id.in_(group))))
            query = select(TelemetryReading, DecisionTelemetryPin.last_slot, DecisionTelemetryPin.reading_id).outerjoin(
                DecisionTelemetryPin, DecisionTelemetryPin.reading_id == TelemetryReading.id).where(
                TelemetryReading.id.in_(group), TelemetryReading.id != maximum)
            for raw, last, pin in db.execute(query):
                if datetime.fromisoformat(raw.received_at) >= boundary_cutoff:
                    continue
                if pin is None or last is not None and last < cutoff_slot:
                    if _raw_signature(raw) != receipts[str(raw.id)]:
                        raise ValueError('Raw row changed after archival; nothing deleted')
                    proposed.append(raw.id)
                    if len(proposed) == policy.delete_batch:
                        break
            if len(proposed) == policy.delete_batch:
                # Remaining receipts stay unchanged until the next bounded tick.
                present_ids.update(int(k) for k in receipts)
                break
    with sessions() as db:
        _begin(db)
        row = db.get(RetentionArchive, start)
        if row.receipts != receipt_copy:
            raise ValueError('Cleanup receipt changed concurrently')
        eligible = []
        for group in chunks(proposed):
            current = db.execute(select(TelemetryReading, DecisionTelemetryPin.last_slot, DecisionTelemetryPin.reading_id)
                .outerjoin(DecisionTelemetryPin, DecisionTelemetryPin.reading_id == TelemetryReading.id)
                .where(TelemetryReading.id.in_(group)))
            for raw, last, pin in current:
                if datetime.fromisoformat(raw.received_at) >= boundary_cutoff:
                    continue
                if pin is not None and (last is None or last >= cutoff_slot):
                    continue
                if _raw_signature(raw) != receipts[str(raw.id)]:
                    raise ValueError('Raw row changed after archival; nothing deleted')
                eligible.append(raw.id)
        for group in chunks(eligible):
            db.execute(delete(DecisionTelemetryPin).where(DecisionTelemetryPin.reading_id.in_(group)))
            db.execute(delete(TelemetryReading).where(TelemetryReading.id.in_(group)))
        removed = set(eligible)
        remaining = {key:value for key,value in receipts.items() if int(key) in present_ids and int(key) not in removed
                     and int(key) != maximum}
        row.receipts = zlib.compress(canonical(remaining)) if remaining else None
        row.status = 'deleting' if eligible and remaining else 'retained-pins' if remaining else 'online-removed'
        state['cleanup_cursor'] = start
        _put_state(db, state)
        db.commit()
        return {'phase':'raw-cleanup', 'start':start, 'deleted':len(eligible), 'remaining':len(remaining)}


def _expire_one(sessions, root, now, policy):
    with sessions() as db:
        row = db.scalar(select(RetentionArchive).where(RetentionArchive.status.in_(('online-removed','expiring')),
            RetentionArchive.end <= iso_time(now-timedelta(days=policy.detail_days)))
            .order_by(RetentionArchive.start).limit(1))
        if not row:
            return None
        path = root / row.manifest
        if not path.resolve().is_relative_to(root.resolve()) or path.name != 'manifest.json':
            raise ValueError('Cold archive expiry path is outside production storage')
        current = root
        for part in path.relative_to(root).parts:
            current /= part
            if current.exists() and (current.is_symlink() or getattr(current.lstat(),'st_file_attributes',0) & 0x400):
                raise ValueError('Cold archive expiry path contains a link')
        if row.status == 'expiring' and not path.exists() and path.parent.exists() and not any(path.parent.iterdir()):
            path.parent.rmdir()
        if row.status == 'expiring' and not path.parent.exists():
            key = row.start
            with sessions() as writer:
                _begin(writer)
                writer.get(RetentionArchive, key).status = 'expired'
                writer.get(RetentionArchive, key).point_ranges = None
                writer.commit()
            return {'phase':'detail-expired', 'start':key}
        path = _manifest(root, row)
        metadata = json.loads(path.read_bytes())
        # Unknown extra files or any link/reparse point prevent directory cleanup.
        table_dir = path.parent/'tables'
        top = list(path.parent.iterdir())
        if ({f.name for f in top}-{'manifest.json','manifest.sha256','tables'} or
                table_dir.exists() and (table_dir.is_symlink() or getattr(table_dir.lstat(), 'st_file_attributes',0) & 0x400)):
            raise ValueError('Archive expiry found an unexpected directory or link')
        files = [f for f in top if f.name != 'tables']+(list(table_dir.iterdir()) if table_dir.exists() else [])
        expected = {'manifest.json','manifest.sha256', *(item['path'] for item in metadata['tables'])}
        if {f.relative_to(path.parent).as_posix() for f in files}-expected or any(f.is_symlink() or f.is_dir() or
            getattr(f.lstat(), 'st_file_attributes', 0) & 0x400 for f in files):
            raise ValueError('Archive expiry found an unexpected file or link')
        key = row.start
    with sessions() as db:
        _begin(db)
        row = db.get(RetentionArchive, key)
        row.status = 'expiring'
        db.commit()
    # Only individually named, validated files. Never recursive removal.
    for file in files:
        if file.name != 'manifest.json':
            file.unlink(missing_ok=True)
    if table_dir.exists():
        table_dir.rmdir()
    path.unlink()
    path.parent.rmdir()
    with sessions() as db:
        _begin(db)
        row = db.get(RetentionArchive, key)
        row.status = 'expired'
        row.receipts = None
        row.point_ranges = None
        db.commit()
    return {'phase':'detail-expired', 'start':key}


def run_tick(sessions, now=None, policy=None):
    policy, now = policy or RetentionPolicy(), now or utc_now()
    engine = sessions.kw['bind']
    assert_writable(engine)
    root = storage_root(engine)
    with maintenance_lock(root) as locked:
        if not locked:
            return {'phase':'busy'}
        with sessions() as db:
            state = _state(db)
        if not state.get('pin_scan_complete'):
            return _pin_scan(sessions, state, now)
        cleanup = _delete_raw_batch(sessions, root, state, policy)
        expired = _expire_one(sessions, root, now, policy)
        cutoff = _hour(now-timedelta(days=policy.online_days))
        with sessions() as db:
            pending = db.scalars(select(RetentionArchive.start).where(RetentionArchive.status == 'deleting').limit(4)).all()
            start = _next_hour(db, state.get('archive_cursor'), iso_time(cutoff)) if len(pending) < 4 else None
        if start is not None and start+timedelta(hours=1) <= cutoff:
            prepared = _prepare(sessions, start, now, policy)
            return {**_publish(sessions, root, prepared, state, now, policy), 'cleanup':cleanup}
        return cleanup or expired or {'phase':'idle', 'policy':policy.as_dict()}
