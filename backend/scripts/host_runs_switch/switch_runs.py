"""Host-local switch of the two confirmed desliming inputs; no PLC writes."""
import argparse
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from urllib.request import Request, urlopen

# Preflight imports must not create bytecode in the host installation.
sys.dont_write_bytecode = True

SOURCE_ID = 'kepserver-main'
MARKER_KEY = 'desliming_runs_cutover'
VERSION = 'desliming-runs-v1'
OLD_POINTS = {
    'tag_3a82e49b2a019451078b': 'TIA_3_3.TIA_3_3.473/if_prod',
    'tag_caf82182c2980109b2de': 'TIA_3_3.TIA_3_3.474/if_prod',
}
RUNS_POINTS = {
    'tag_881030838dd4d05ebcda': 'TIA_3_3.TIA_3_3.473/RUNS',
    'tag_04a29ab776cb07869148': 'TIA_3_3.TIA_3_3.474/RUNS',
}
SOURCE_FILES = frozenset({
    'backend/app/telemetry/derived.py', 'backend/app/hourly_brief/inputs.py',
    'backend/app/services/decision_sources.py', 'backend/app/decision_hourly/history.py',
})
VARIANT_SOURCE = 'backend/app/telemetry/derived.py'
VARIANT_SOURCES = {
    'variants/derived-6dd6411.py': VARIANT_SOURCE,
    'variants/derived-6dd6411-crlf.py': VARIANT_SOURCE,
    'variants/derived-host-1a00abe3.py': VARIANT_SOURCE,
    'variants/inputs-host-1c03e358.py': 'backend/app/hourly_brief/inputs.py',
}
VARIANT_FILES = tuple(VARIANT_SOURCES)
CAPTURED_VARIANTS = frozenset({'variants/derived-host-1a00abe3.py', 'variants/inputs-host-1c03e358.py'})
BUNDLE_FILES = frozenset({
    'switch_runs.py', 'SwitchDeslimingRuns.cmd', 'collector_control.py',
    'StopOwnedWeb.ps1', 'StopOwnedCollector.ps1', 'README.txt',
    *('payload/' + name for name in SOURCE_FILES),
    *VARIANT_FILES,
})


class SwitchError(RuntimeError):
    pass


def strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SwitchError('Duplicate JSON fields; nothing was changed')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def source_facts(name, raw):
    """Small non-secret evidence for an unknown, still-rejected source file."""
    crlf = raw.count(b'\r\n')
    return dict(path=name, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw),
        utf8_bom=raw.startswith(b'\xef\xbb\xbf'), crlf=crlf,
        lf_only=raw.count(b'\n')-crlf, cr_only=raw.count(b'\r')-crlf)


def point_identity(point, key, item):
    return (point.get('key') == key and point.get('item_id') == item
        and point.get('node_id') == 'ns=2;s=' + item and point.get('value_type') == 'boolean'
        and point.get('scale', 1) == 1 and point.get('offset', 0) == 0
        and point.get('minimum') is None and point.get('maximum') is None)


def points_span(text):
    """Locate the top-level array and each original element without reserializing it."""
    decoder = json.JSONDecoder()
    index = text.index('{') + 1
    while True:
        while text[index].isspace(): index += 1
        key, index = decoder.raw_decode(text, index)
        while text[index].isspace(): index += 1
        if text[index] != ':': raise SwitchError('Invalid collector JSON')
        index += 1
        while text[index].isspace(): index += 1
        start = index
        value, end = decoder.raw_decode(text, index)
        if key == 'points' and isinstance(value, list):
            index = start + 1
            spans = []
            while True:
                while text[index].isspace(): index += 1
                if text[index] == ']': return start, end, spans
                element = index
                _, index = decoder.raw_decode(text, index)
                spans.append((element, index))
                while text[index].isspace(): index += 1
                if text[index] == ',': index += 1
                elif text[index] != ']': raise SwitchError('Invalid points array')
        index = end
        while text[index].isspace(): index += 1
        if text[index] != ',': raise SwitchError('Collector points array is missing')
        index += 1


def plan_config(original):
    from app.telemetry.config import CollectorConfig
    text = original.decode('utf-8-sig')
    data = strict_json(text)
    cfg = CollectorConfig.model_validate(data)
    if (not cfg.enabled or cfg.source_id != SOURCE_ID or cfg.poll_seconds != 5
            or cfg.history_interval_seconds != 60 or cfg.level_history_interval_seconds != 30):
        raise SwitchError('Expected host 5-second polling, 60-second history and 30-second level policy')
    for key, item in RUNS_POINTS.items():
        found = [p for p in data['points'] if any(p.get(k) == v for k, v in
            (('key', key), ('item_id', item), ('node_id', 'ns=2;s=' + item)))]
        if len(found) != 1 or not point_identity(found[0], key, item) or found[0].get('max_age_seconds') != 30:
            raise SwitchError('The two previously installed RUNS definitions are missing or conflict')
    removed = []
    for key, item in OLD_POINTS.items():
        found = [p for p in data['points'] if any(p.get(k) == v for k, v in
            (('key', key), ('item_id', item), ('node_id', 'ns=2;s=' + item)))]
        if found and (len(found) != 1 or not point_identity(found[0], key, item)):
            raise SwitchError('Retired if_prod identity conflicts; configuration was preserved')
        if found: removed.append(key)
    if len(removed) == 1:
        raise SwitchError('Only one retired definition is present; partial state needs review')
    if not removed: return original, cfg, []
    start, end, spans = points_span(text)
    kept = [text[a:b] for p, (a, b) in zip(data['points'], spans) if p['key'] not in OLD_POINTS]
    newline = '\r\n' if '\r\n' in text else '\n'
    replacement = '[' + newline + '    ' + (',' + newline + '    ').join(kept) + newline + '  ]'
    updated_text = text[:start] + replacement + text[end:]
    parsed = strict_json(updated_text)
    CollectorConfig.model_validate(parsed)
    if (parsed['points'] != [p for p in data['points'] if p['key'] not in OLD_POINTS]
            or {k:v for k,v in parsed.items() if k != 'points'} != {k:v for k,v in data.items() if k != 'points'}):
        raise SwitchError('Unexpected configuration delta')
    updated = (b'\xef\xbb\xbf' if original.startswith(b'\xef\xbb\xbf') else b'') + updated_text.encode('utf-8')
    return updated, cfg, removed


def database_path(root):
    return (root / 'backend/data/dense_medium.db').resolve()


def database_state(root, *, decode=None):
    if decode is None:
        from point_metadata import Catalog, DIRECTORY
        decode = Catalog(database_path(root).parent / DIRECTORY, compact_writes=False).loads
    with closing(sqlite3.connect(database_path(root).as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        marker = db.execute('SELECT value,value_type,updated_at FROM settings WHERE key=?', (MARKER_KEY,)).fetchone()
        revision = db.execute('SELECT id,effective_at,config FROM decision_source_revisions ORDER BY id DESC LIMIT 1').fetchone()
        points = {}
        for key, item in {**OLD_POINTS, **RUNS_POINTS}.items():
            row = db.execute('SELECT config,enabled FROM telemetry_points WHERE source_id=? AND key=?', (SOURCE_ID, key)).fetchone()
            if row is None or not point_identity(decode(row[0]), key, item):
                raise SwitchError('Expected exact original point metadata is missing or conflicts')
            points[key] = [row[0], row[1]]
    return dict(marker=list(marker) if marker else None, revision=list(revision) if revision else None, points=points)


def plan_database(state, removed):
    marker = strict_json(state['marker'][0]) if state['marker'] else None
    if state['marker'] is not None:
        try:
            valid = (isinstance(marker, dict) and marker['version'] == VERSION and marker['source_id'] == SOURCE_ID
                and datetime.fromisoformat(marker['effective_at']).utcoffset() is not None
                and datetime.fromisoformat(marker['effective_at']) <= datetime.now(timezone.utc))
        except (KeyError, TypeError, ValueError): valid = False
        if not valid: raise SwitchError('Existing desliming cutover marker is invalid; it was preserved')
    if bool(removed) == bool(marker):
        raise SwitchError('Configuration and cutover marker disagree; partial state needs review')
    for key, (_, enabled) in state['points'].items():
        expected = key in RUNS_POINTS or bool(removed)
        if bool(enabled) != expected:
            raise SwitchError('Point registry and active configuration disagree; nothing was changed')
    original = strict_json(state['revision'][2]) if state['revision'] else dict(choices={}, target_min=8.5, target_max=9.0)
    updated = deepcopy(original)
    choice = original.get('choices', {}).get('desliming')
    if choice:
        try: source, key = strict_json(choice)
        except (ValueError, TypeError): raise SwitchError('Current desliming binding is invalid') from None
        if source == SOURCE_ID and key in OLD_POINTS:
            updated['choices']['desliming'] = json.dumps([SOURCE_ID, 'desliming_status'], separators=(',', ':'))
    if marker and updated != original:
        raise SwitchError('A retired explicit binding was reintroduced after cutover; review is required')
    return dict(already_switched=bool(marker), config=updated if updated != original else None)


def write_database(root, expected, planned, effective_at):
    """One small transaction; history, models, assays and all prior revisions are untouched."""
    marker = dict(version=VERSION, source_id=SOURCE_ID, effective_at=effective_at)
    encoded = json.dumps(marker, separators=(',', ':'))
    updated_at = datetime.now().isoformat(sep=' ', timespec='microseconds')
    inserted = None
    with closing(sqlite3.connect(database_path(root).as_uri() + '?mode=rw', uri=True, timeout=5)) as db:
        db.execute('BEGIN IMMEDIATE')
        if database_state(root) != expected:
            raise SwitchError('Point metadata or source choices changed before the switch; nothing was overwritten')
        db.execute('INSERT INTO settings(key,value,value_type,updated_at) VALUES(?,?,?,?)', (MARKER_KEY, encoded, 'json', updated_at))
        if planned['config'] is not None:
            config = json.dumps(planned['config'], ensure_ascii=False, separators=(',', ':'))
            cursor = db.execute('INSERT INTO decision_source_revisions(effective_at,config) VALUES(?,?)', (effective_at, config))
            inserted = [cursor.lastrowid, effective_at, config]
        for key in OLD_POINTS:
            db.execute('UPDATE telemetry_points SET enabled=0 WHERE source_id=? AND key=?', (SOURCE_ID, key))
        db.commit()
    return dict(marker=[encoded, 'json', updated_at], inserted_revision=inserted)


def rollback_database(root, before, written):
    with closing(sqlite3.connect(database_path(root).as_uri() + '?mode=rw', uri=True, timeout=5)) as db:
        db.execute('BEGIN IMMEDIATE')
        current = database_state(root)
        expected_revision = written['inserted_revision'] or before['revision']
        if current['marker'] != written['marker'] or current['revision'] != expected_revision:
            raise SwitchError('Source settings changed externally; automatic rollback was blocked')
        if any(current['points'][key][0] != before['points'][key][0] for key in before['points']):
            raise SwitchError('Point definitions changed externally; automatic rollback was blocked')
        db.execute('DELETE FROM settings WHERE key=?', (MARKER_KEY,))
        if written['inserted_revision']:
            db.execute('DELETE FROM decision_source_revisions WHERE id=?', (written['inserted_revision'][0],))
        for key, (_, enabled) in before['points'].items():
            db.execute('UPDATE telemetry_points SET enabled=? WHERE source_id=? AND key=?', (enabled, SOURCE_ID, key))
        db.commit()


def atomic_file(path, data):
    temporary = path.with_name(path.name + '.runs-' + str(time.time_ns()) + '.tmp')
    try:
        with temporary.open('xb') as output:
            output.write(data); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    if path.suffix == '.py':
        Path(importlib.util.cache_from_source(str(path))).unlink(missing_ok=True)


@contextmanager
def update_lock(root):
    # Share the lock with the already delivered append-only RUNS installer.
    path = root / 'backend/data/.runs-points-update.lock'
    with path.open('a+b') as stream:
        stream.seek(0, 2)
        if not stream.tell(): stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SwitchError('Another RUNS update is running') from None
        try: yield
        finally:
            if os.name == 'nt': stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def start_collector(root):
    env = os.environ.copy()
    env['DMCS_DATABASE_URL'] = 'sqlite:///' + database_path(root).as_posix()
    env['DMCS_TELEMETRY_SPOOL'] = str(root / 'backend/data/telemetry-spool')
    result = subprocess.run([sys.executable, str(root / 'backend/scripts/start_collector.py'),
        '--config', str(root / 'backend/config/kepserver.local.json')], cwd=root, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
    if result.returncode: raise SwitchError('Collector launcher failed; private output was not printed')


def start_web(root, control):
    control.powershell(root / 'backend/scripts/start_server.ps1', '-NoBrowser', cwd=root)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            with urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3) as response:
                if json.load(response).get('status') == 'ok': return
        except (OSError, ValueError): pass
        time.sleep(1)
    raise SwitchError('Web service health was not confirmed')


def verify_samples(root, since, *, timeout=120, decode=None):
    from app.telemetry.domain import definition_id
    if decode is None:
        from point_metadata import Catalog, DIRECTORY
        decode = Catalog((root / 'backend/data').resolve() / DIRECTORY, compact_writes=False).loads
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = {}
        identities = []
        with closing(sqlite3.connect(database_path(root).as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            for key, item in RUNS_POINTS.items():
                definition = db.execute('SELECT config FROM telemetry_points WHERE source_id=? AND key=?', (SOURCE_ID, key)).fetchone()
                if definition is None: raise SwitchError('RUNS metadata is missing')
                expected_definition = definition_id(decode(definition[0]))
                row = db.execute('SELECT received_at,sample,batch_id FROM telemetry_readings INDEXED BY ix_telemetry_point_time '
                    'WHERE source_id=? AND point_key=? AND received_at>=? ORDER BY received_at DESC LIMIT 1', (SOURCE_ID, key, since)).fetchone()
                if row:
                    sample = decode(row[1])
                    code = sample.get('status_code')
                    if (type(sample.get('value')) is not bool or sample.get('quality') != 'good' or sample.get('issue')
                            or type(code) is not int or not 0 <= code <= 0xFFFFFFFF or code & 0xC0000000
                            or sample.get('received_at') != row[0] or sample.get('key') != key
                            or sample.get('node_id') != 'ns=2;s=' + item or sample.get('definition_id') != expected_definition):
                        raise SwitchError('Fresh RUNS sample has an invalid type, quality or identity')
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(row[0].replace('Z', '+00:00'))).total_seconds()
                    if not 0 <= age <= 90: raise SwitchError('RUNS sample is stale or has a future receipt time')
                    found[key] = row[0]
                    identities.append((row[0], row[2]))
        if len(found) == 2:
            if not identities[0][1] or len(set(identities)) != 1:
                raise SwitchError('RUNS states do not belong to the same raw batch and receipt time')
            return found
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise SwitchError('Timed out waiting for fresh Good Boolean RUNS samples')


def verify_active(root, control):
    cfg = control.collector_configuration(root)
    status = control.collector_status(root, SOURCE_ID)
    now = datetime.now(timezone.utc)
    try:
        healthy = (status.get('state') == 'connected' and status.get('owner')
            and datetime.fromisoformat(status['lease_until'].replace('Z', '+00:00')) > now
            and datetime.fromisoformat(status['last_sample_at'].replace('Z', '+00:00')) >= now - timedelta(seconds=90))
    except (KeyError, TypeError, ValueError): healthy = False
    if not healthy: raise SwitchError('Active collector ownership, lease and fresh committed sample were not confirmed')
    return cfg, status


def check_bundle(package, manifest):
    if (manifest.get('format') != 1 or manifest.get('purpose') != 'desliming-runs-switch-v1'
            or set(manifest.get('files', {})) != BUNDLE_FILES or set(manifest.get('source_before_sha256', {})) != SOURCE_FILES
            or set(manifest.get('variant_before_sha256', {})) != set(VARIANT_FILES)):
        raise SwitchError('Reviewed package scope differs')
    for name, digest in manifest['files'].items():
        if hashlib.sha256((package / name).read_bytes()).hexdigest() != digest: raise SwitchError('Package is incomplete or damaged')


def preflight(root, package, manifest):
    if (os.environ.get('DMCS_REMOTE_READONLY_CONFIG') or (root / 'backend/data/remote-readonly.json').exists()
            or (root / 'backend/data/client-mode.json').exists()):
        raise SwitchError('Read-only laptop/client installation cannot perform this host switch')
    required = ['offline-install.json', 'backend/config/kepserver.local.json', 'backend/data/dense_medium.db',
        'backend/scripts/start_collector.py', 'backend/scripts/start_server.ps1']
    if not all((root / name).is_file() for name in required): raise SwitchError('Recognized host installation is missing')
    check_bundle(package, manifest)
    sources = {}
    for name in SOURCE_FILES:
        path = root / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            raise SwitchError('Expected ordinary source file is missing or redirected')
        original = path.read_bytes()
        target = (package / 'payload' / name).read_bytes()
        allowed = manifest['source_before_sha256'][name]
        original_hash = hashlib.sha256(original).hexdigest()
        matching = []
        for variant_name, source_name in VARIANT_SOURCES.items():
            if source_name == name:
                variant = (package / variant_name).read_bytes()
                variant_allowed = manifest['variant_before_sha256'][variant_name]
                if not isinstance(variant_allowed, list): raise SwitchError('Invalid reviewed source variant')
                if original_hash in variant_allowed + [hashlib.sha256(variant).hexdigest()]:
                    matching.append((variant, variant_allowed, variant_name))
        if len(matching) > 1: raise SwitchError('Ambiguous reviewed source variants')
        selected_variant = None
        if matching: target, allowed, selected_variant = matching[0]
        if not isinstance(allowed, list) or original_hash not in allowed + [hashlib.sha256(target).hexdigest()]:
            facts = json.dumps(source_facts(name, original), separators=(',', ':'))
            raise SwitchError('Unknown installed source version: ' + name + '; no file was overwritten; actual=' + facts)
        compile(target, name, 'exec')
        sources[name] = dict(original=original, updated=target, variant=selected_variant)
    captured = [source for source in sources.values() if source['variant'] in CAPTURED_VARIANTS]
    if captured and len(captured) != len(CAPTURED_VARIANTS):
        raise SwitchError('Captured host derived/Inputs variants must be selected together; no file was overwritten')
    if captured and len({source['original'] == source['updated'] for source in captured}) != 1:
        raise SwitchError('Captured host derived/Inputs pair is partially updated; preserve the journal for review')
    config = root / 'backend/config/kepserver.local.json'
    original = config.read_bytes()
    updated, cfg, removed = plan_config(original)
    state = database_state(root)
    planned = plan_database(state, removed)
    if planned['already_switched'] and any(s['original'] != s['updated'] for s in sources.values()):
        raise SwitchError('Cutover marker exists but reviewed runtime sources differ; partial state needs review')
    return dict(original=original, updated=updated, removed=removed, state=state, planned=planned, sources=sources)


def apply(root, package, manifest, control, report=lambda *args: None, *, dry_run=False):
    initial = preflight(root, package, manifest)
    verify_active(root, control)
    since = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat(timespec='milliseconds')
    verify_samples(root, since, timeout=.1)
    if dry_run:
        return dict(status='preflight-passed', removed=initial['removed'], already_switched=initial['planned']['already_switched'])
    with update_lock(root):
        task = preflight(root, package, manifest)
        cfg, before_status = verify_active(root, control)
        if task['planned']['already_switched']:
            since = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat(timespec='milliseconds')
            samples = verify_samples(root, since, timeout=.1)
            return dict(status='unchanged-verified', cutover=strict_json(task['state']['marker'][0]), fresh_samples=samples)
        # The new points must already be usable before any existing service is stopped.
        since = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat(timespec='milliseconds')
        verify_samples(root, since, timeout=.1)
        backup = root / 'backups' / ('runs-switch-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        backup.mkdir(parents=True)
        atomic_file(backup / 'kepserver.local.json', task['original'])
        for name, source in task['sources'].items():
            path = backup / 'source' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_file(path, source['original'])
        journal = dict(status='prepared', before=task['state'], written=None)
        atomic_file(backup / 'journal.json', (json.dumps(journal, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        written = None
        changed = []
        config = root / 'backend/config/kepserver.local.json'
        try:
            report('copying', 'Source/configuration and affected metadata backed up; database history, models and spool are retained.')
            control.powershell(package / 'StopOwnedWeb.ps1', '-InstallDir', str(root), cwd=root)
            control.powershell(package / 'StopOwnedCollector.ps1', '-InstallDir', str(root), cwd=root)
            control.wait_collector_lease(root, cfg, report)
            if config.read_bytes() != task['original'] or database_state(root) != task['state']:
                raise SwitchError('Configuration or bindings changed while services stopped; no overwrite was allowed')
            for name, source in task['sources'].items():
                if (root / name).read_bytes() != source['original']:
                    raise SwitchError('Installed source changed externally: ' + name)
            for name, source in task['sources'].items():
                if source['original'] != source['updated']:
                    changed.append(name)
                    atomic_file(root / name, source['updated'])
            atomic_file(config, task['updated'])
            cutover = timestamp()
            written = write_database(root, task['state'], task['planned'], cutover)
            journal.update(status='switched-verifying', written=written)
            atomic_file(backup / 'journal.json', (json.dumps(journal, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
            report('starting', 'Starting the owned collector; 473/474 use RUNS and only their retired if_prod definitions are inactive.')
            start_collector(root)
            control.collector_ready(root, cfg, before_status, report)
            samples = verify_samples(root, cutover)
            after = database_state(root)
            if (after['marker'] != written['marker']
                    or after['revision'] != (written['inserted_revision'] or task['state']['revision'])
                    or any(after['points'][key][0] != task['state']['points'][key][0] for key in after['points'])
                    or any(bool(after['points'][key][1]) != (key in RUNS_POINTS)
                    for key in {**OLD_POINTS, **RUNS_POINTS})):
                raise SwitchError('Cutover metadata or point activation changed during verification')
            if config.read_bytes() != task['updated'] or any((root/name).read_bytes() != source['updated']
                    for name,source in task['sources'].items()):
                raise SwitchError('Configuration or runtime source changed during verification')
            start_web(root, control)
            verify_active(root, control)
            samples = verify_samples(root, cutover, timeout=.1)
            if config.read_bytes() != task['updated'] or any((root/name).read_bytes() != source['updated']
                    for name,source in task['sources'].items()):
                raise SwitchError('Configuration or runtime source changed while Web restarted')
        except BaseException:
            report('rolling-back', 'RUNS switch failed; restoring only this component source/configuration and metadata changes.')
            recovery_before = control.collector_status(root, SOURCE_ID)
            control.powershell(package / 'StopOwnedWeb.ps1', '-InstallDir', str(root), cwd=root)
            control.powershell(package / 'StopOwnedCollector.ps1', '-InstallDir', str(root), cwd=root)
            control.wait_collector_lease(root, cfg, report)
            if config.read_bytes() not in (task['original'], task['updated']):
                raise SwitchError('Configuration changed externally; rollback and restart were blocked')
            for name in changed:
                if (root / name).read_bytes() not in (task['sources'][name]['original'], task['sources'][name]['updated']):
                    raise SwitchError('Source changed externally; rollback and restart were blocked: ' + name)
            if written: rollback_database(root, task['state'], written)
            elif database_state(root) != task['state']:
                raise SwitchError('Metadata changed externally; rollback and restart were blocked')
            atomic_file(config, task['original'])
            for name in changed: atomic_file(root / name, task['sources'][name]['original'])
            start_collector(root)
            control.collector_ready(root, cfg, recovery_before, report)
            start_web(root, control)
            journal['status'] = 'rolled-back'
            atomic_file(backup / 'journal.json', (json.dumps(journal, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
            raise
        result = dict(status='installed', cutover=dict(version=VERSION, source_id=SOURCE_ID, effective_at=cutover),
            removed=list(OLD_POINTS), fresh_samples=samples, configuration_sha256=hashlib.sha256(task['updated']).hexdigest())
        journal.update(status='installed', result=result)
        atomic_file(backup / 'journal.json', (json.dumps(journal, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-dir', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--component', action='store_true', help='Leave overall installed reporting to the combined wrapper')
    args = parser.parse_args()
    root, package = args.install_dir.resolve(), Path(__file__).resolve().parent
    sys.path.insert(0, str(root / 'backend'))
    manifest = strict_json((package / 'bundle.json').read_text(encoding='utf-8'))
    check_bundle(package, manifest)
    spec = importlib.util.spec_from_file_location('runs_switch_control', package / 'collector_control.py')
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)
    def report(stage, message):
        print('RUNS ' + stage + ': ' + message, flush=True)
        if args.dry_run: return
        try:
            callback_stage = 'copying' if stage == 'rolling-back' or stage == 'installed' and args.component else stage
            body = json.dumps(dict(stage=callback_stage, message='[RUNS component] ' + message,
                healthy=stage == 'installed' and not args.component,
                revision=manifest['version'], at=datetime.now().astimezone().isoformat())).encode()
            with urlopen(Request(manifest['report_url'], data=body, headers={'Content-Type':'application/json'}), timeout=4): pass
        except OSError: pass
    try:
        result = apply(root, package, manifest, control, report, dry_run=args.dry_run)
        report('verified' if args.dry_run else 'installed', result['status'] + ': confirmed 473/474 RUNS input switch.')
        return 0
    except Exception as exc:
        report('failed', str(exc) if isinstance(exc, SwitchError) else type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
