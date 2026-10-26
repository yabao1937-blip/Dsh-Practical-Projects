"""Bounded, replaceable collector IPC; never a durable history or audit source."""
from copy import deepcopy
from datetime import timedelta
import json
import math
import os
from pathlib import Path
import re

from sqlalchemy import select

from .domain import definition_id, iso_time, parse_time, utc_now
from .models import TelemetrySource
from .retained import usable_at_receipt
from .density_effective import valid_density

RING_SECONDS = 180
MAX_BATCHES = 128
MAX_BYTES = 4 * 1024 * 1024
ALIGNMENT_LEVEL_ITEM = 'TIA_3_5.TIA_3_5.JMCWC/LEVEL_B'
ALIGNMENT_LAG_SECONDS = 90
ALIGNMENT_LOOKBACK_SECONDS = 30
ALIGNMENT_EVIDENCE_CADENCE = 5
BOUNDARY_ITEMS = {f'TIA_4_1.TIA_4_1.{channel}/{kind}' for channel in ('A303', 'A304') for kind in ('MD', 'MD_SET')}


def select_hour_boundary_evidence(document, cfg, owner, prediction_at):
    """Keep the latest original pre-hour density pair, including bad quality."""
    boundary = prediction_at.replace(minute=0, second=0, microsecond=0)
    if (cfg.history_interval_seconds != 60 or not 0 <= (prediction_at-boundary).total_seconds() < 60
            or not isinstance(document, dict) or document.get('owner') != owner
            or document.get('source_id') != cfg.source_id or document.get('state') != 'connected'):
        return []
    result = []
    for channel in ('A303', 'A304'):
        points = [p for p in cfg.points if p.item_id in {f'TIA_4_1.TIA_4_1.{channel}/MD', f'TIA_4_1.TIA_4_1.{channel}/MD_SET'}]
        if len(points) != 2 or len({p.item_id for p in points}) != 2:
            continue
        keys = {p.key for p in points}
        candidates = []
        for batch in document.get('batches', []):
            if batch.get('source_id') != cfg.source_id or not re.fullmatch(r'[a-f0-9]{32}', batch.get('batch_id') or ''):
                continue
            samples = [s for s in batch.get('samples', []) if s.get('key') in keys]
            try:
                latest = max(parse_time(s['received_at']) for s in samples)
                if boundary-timedelta(seconds=30) <= latest < boundary:
                    candidates.append((latest, batch['batch_id'], samples))
            except (KeyError, TypeError, ValueError):
                continue
        if not candidates:
            continue
        received, batch_id, samples = max(candidates, key=lambda c:c[0])
        if len(samples) != 2 or {s['key'] for s in samples} != keys:
            continue
        by_key = {s['key']:s for s in samples}
        if any(by_key[p.key].get('runtime_only') or by_key[p.key].get('definition_id') != definition_id(p.model_dump())
               or parse_time(by_key[p.key]['received_at']) != received for p in points):
            continue
        for point in points:
            result.append(dict(source_id=cfg.source_id, point_key=point.key, item_id=point.item_id,
                point_config=point.model_dump(),
                batch_id=batch_id, captured_at=iso_time(prediction_at), hour_end_at=iso_time(boundary),
                storage_kind='hour-boundary-evidence', sample=dict(deepcopy(by_key[point.key]),
                storage_kind='hour-boundary-evidence', cadence_seconds=5)))
    return result


def select_alignment_evidence(document, cfg, owner, prediction_at):
    """Capture at most one original level sample with a minute's durable batch.

    The ring remains replaceable; only this selected raw event is made durable.
    Quality is deliberately not a filter: a bad latest event must remain bad.
    """
    if (cfg.history_interval_seconds != 60 or not isinstance(document, dict)
            or document.get('source_id') != cfg.source_id or document.get('owner') != owner
            or document.get('state') != 'connected'):
        return []
    points = [point for point in cfg.points if point.item_id == ALIGNMENT_LEVEL_ITEM]
    if len(points) != 1:
        return []
    point = points[0]
    identity = definition_id(point.model_dump())
    target = prediction_at - timedelta(seconds=ALIGNMENT_LAG_SECONDS)
    lower = target - timedelta(seconds=ALIGNMENT_LOOKBACK_SECONDS)
    candidates = []
    for batch in document.get('batches', []):
        if batch.get('source_id') != cfg.source_id:
            continue
        batch_id = batch.get('batch_id')
        if not isinstance(batch_id, str) or not re.fullmatch(r'[a-f0-9]{32}', batch_id):
            continue
        for sample in batch.get('samples', []):
            if (sample.get('key') != point.key or sample.get('definition_id') != identity
                    or sample.get('runtime_only')):
                continue
            try:
                received = parse_time(sample['received_at'])
                if lower <= received <= target:
                    candidates.append((received, batch_id, sample))
            except (KeyError, ValueError, TypeError):
                continue
    if not candidates:
        return []
    _, batch_id, sample = max(candidates, key=lambda candidate: candidate[0])
    return [dict(source_id=cfg.source_id, point_key=point.key, batch_id=batch_id,
        item_id=ALIGNMENT_LEVEL_ITEM, prediction_at=iso_time(prediction_at), target_at=iso_time(target),
        storage_kind='alignment-evidence',
        sample=dict(deepcopy(sample), storage_kind='alignment-evidence',
                    cadence_seconds=ALIGNMENT_EVIDENCE_CADENCE))]


def runtime_folder(bind):
    """A separate runtime namespace per local database; remote facades have none."""
    url = getattr(bind, 'url', None)
    database = getattr(url, 'database', None)
    if not url or url.get_backend_name() != 'sqlite' or not database or database == ':memory:' or database.startswith('file:'):
        return None
    path = Path(database).resolve()
    return path.parent / (path.name + '.runtime') / 'telemetry'


def _path(folder, source):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', source):
        raise ValueError('invalid runtime source')
    return folder / (source + '.json')


def _fresh(data, now):
    try:
        if not isinstance(data, dict):
            return False
        poll = data.get('poll_seconds')
        valid = (isinstance(poll, (float, int)) and not isinstance(poll, bool)
            and math.isfinite(poll) and .1 <= poll <= 60 and data.get('version') == 1
            and -5 <= (now - parse_time(data['published_at'])).total_seconds() <= max(15, poll * 3)
            and isinstance(data.get('batches'), list) and len(data['batches']) <= MAX_BATCHES)
        if not valid:
            return False
        for batch in data['batches']:
            if (not isinstance(batch, dict) or batch.get('source_id') != data.get('source_id')
                    or not isinstance(batch.get('batch_id'), str)
                    or not isinstance(batch.get('samples'), list) or len(batch['samples']) > 500):
                return False
            for sample in batch['samples']:
                if not isinstance(sample, dict) or not isinstance(sample.get('key'), str):
                    return False
                parse_time(sample['received_at'])
        return True
    except (ValueError, KeyError, TypeError):
        return False


def read_runtime_path(database_path, when=None):
    """Read fresh IPC transport documents; the consumer must still fence owners."""
    path = Path(database_path).resolve()
    folder = path.parent / (path.name + '.runtime') / 'telemetry'
    now, result = when or utc_now(), {}
    for path in folder.glob('*.json'):
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            data = json.loads(path.read_text(encoding='utf-8'))
            source = data.get('source_id') if isinstance(data, dict) else None
            if isinstance(source, str) and _path(folder, source) == path and _fresh(data, now):
                result[source] = data
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def read_runtime(db, when=None):
    """Return fresh IPC documents keyed by source, fenced by the SQL owner lease.

    No document can extend its own lease. A laptop remote-SQL facade deliberately
    has no path, so it cannot accidentally read a local production cache.
    """
    folder = runtime_folder(db.get_bind())
    supplied = db.info.get('telemetry_runtime')
    if folder is None and not isinstance(supplied, dict):
        return {}
    now, result = when or utc_now(), {}
    for source in db.scalars(select(TelemetrySource)):
        try:
            if not source.owner or not source.lease_until or parse_time(source.lease_until) <= now:
                continue
            if isinstance(supplied, dict):
                data = supplied.get(source.source_id)
            else:
                path = _path(folder, source.source_id)
                if path.stat().st_size > MAX_BYTES:
                    continue
                data = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(data, dict) or not _fresh(data, now) or data.get('source_id') != source.source_id
                    or data.get('owner') != source.owner):
                continue
            result[source.source_id] = data
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def cached_samples(runtime, point, when=None):
    """Same-definition raw observations, marked explicitly as ephemeral."""
    data = runtime.get(point.source_id) or {}
    candidates = []
    identity = definition_id(point.config)
    for batch in data.get('batches', []):
        if not isinstance(batch, dict):
            continue
        for sample in batch.get('samples', []):
            try:
                if (sample.get('key') == point.key and sample.get('definition_id') == identity
                        and (when is None or parse_time(sample['received_at']) <= when)):
                    candidates.append(dict(sample, batch_id=batch['batch_id'], runtime_only=True))
            except (KeyError, TypeError, ValueError):
                continue
    return candidates


def cached_memory(runtime, point, kind, when=None):
    data = runtime.get(point.source_id) or {}
    memory = (data.get('memories') or {}).get(point.key) or {}
    sample = memory.get(kind)
    try:
        if (memory.get('definition_id') == definition_id(point.config) and sample
                and usable_at_receipt(sample, point.config)
                and (when is None or parse_time(sample['received_at']) <= when)):
            return dict(sample, runtime_only=True)
    except (KeyError, TypeError, ValueError):
        pass
    return None


class RuntimePublisher:
    def __init__(self, folder, cfg, owner):
        self.folder, self.cfg = folder, cfg
        self.document = dict(version=1, source_id=cfg.source_id, owner=owner,
            poll_seconds=cfg.poll_seconds, cadence_seconds=cfg.history_interval_seconds,
            state='connecting', error_code=None, retry_seconds=0, batches=[], memories={})

    @classmethod
    def for_repository(cls, repository, cfg, owner):
        sessions = getattr(repository, 'sessions', None)
        if sessions is None:
            return None
        with sessions() as db:
            folder = runtime_folder(db.get_bind())
        return cls(folder, cfg, owner) if folder is not None else None

    def publish(self, state, code=None, delay=0, batch=None):
        now = utc_now()
        data = self.document
        if state != 'connected':
            # A new connection must warm its own lag ring. Display memories are
            # retained separately, but cannot become alignment evidence.
            data['batches'] = []
        data.update(state=state, error_code=code, retry_seconds=delay, published_at=iso_time(now))
        if batch:
            data['batches'].append(deepcopy(batch))
            lower = now - timedelta(seconds=RING_SECONDS)
            data['batches'] = [b for b in data['batches']
                if b.get('samples') and parse_time(b['samples'][0]['received_at']) >= lower][-MAX_BATCHES:]
            points = {p.key:p.model_dump() for p in self.cfg.points}
            for sample in batch['samples']:
                config = points.get(sample['key'])
                if not config or not usable_at_receipt(sample, config):
                    continue
                identity = definition_id(config)
                memory = data['memories'].get(sample['key']) or {}
                if memory.get('definition_id') != identity:
                    memory = dict(definition_id=identity)
                saved = dict(sample, batch_id=batch['batch_id'])
                memory['last_valid'] = saved
                if isinstance(sample.get('value'), (float, int)) and not isinstance(sample.get('value'), bool):
                    if sample['value'] != 0:
                        memory['last_nonzero'] = saved
                    if valid_density(sample['value']):
                        memory['last_density'] = saved
                data['memories'][sample['key']] = memory
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
        while len(payload) > MAX_BYTES and len(data['batches']) > 1:
            data['batches'].pop(0)
            payload = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
        if len(payload) > MAX_BYTES:
            raise ValueError('runtime snapshot exceeds bounded capacity')
        self.folder.mkdir(parents=True, exist_ok=True)
        target = _path(self.folder, self.cfg.source_id)
        temporary = target.with_suffix('.' + self.document['owner'] + '.tmp')
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
