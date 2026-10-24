"""Verified source update; only an explicit, backed-up history setting may change."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import time
from urllib.request import Request, urlopen


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def allowed(name):
    return (name.startswith(('frontend/','backend/app/','backend/migrations/')) or name in {
        'backend/client_mode.py','backend/serve.py','backend/collector.py',
        'backend/scripts/storage_retention.py',
        'backend/scripts/start_server.ps1','backend/scripts/start_collector.py','backend/scripts/watchdog_server.ps1'})


def safe_path(root,name):
    parts = PurePosixPath(name)
    if '\\' in name or ':' in name or parts.is_absolute() or '..' in parts.parts or not allowed(name):
        raise ValueError('Forbidden update path: '+name)
    path = (root/Path(*parts.parts)).resolve()
    if not path.is_relative_to(root): raise ValueError('Path escapes installation')
    return path


def collector_changed(manifest):
    """These files are imported by the long-lived collector or define its schema."""
    return manifest.get('profile') != 'query-only-hotfix' and ('collector_history_interval_seconds' in manifest or any(
        name.startswith(('backend/app/telemetry/','backend/migrations/')) or name in {
            'backend/collector.py','backend/client_mode.py','backend/scripts/start_collector.py',
            'backend/app/database.py','backend/app/config.py','backend/app/models.py',
            'backend/app/schema_upgrade.py','backend/app/decision_hourly/models.py'}
        for name in manifest['files']))


def history_configuration_patch(root,manifest):
    """Prepare one fixed setting change, preserving every other config byte.

    No credential, endpoint, point or polling setting can be supplied by an
    update manifest. The original bytes are also used for race detection and
    exact rollback, rather than reconstructing a production configuration.
    """
    if 'collector_history_interval_seconds' not in manifest: return None
    interval = manifest['collector_history_interval_seconds']
    if manifest.get('profile') is not None or type(interval) is not int or interval not in (30,60):
        raise ValueError('Collector history interval patch requires a standard update and the integer value 30 or 60')
    path = root/'backend/config/kepserver.local.json'
    try:
        original = path.read_bytes()
        text = original.decode('utf-8-sig')
        data = json.loads(text)
        if not isinstance(data,dict): raise ValueError()
        poll = data.get('poll_seconds',5)
        if (isinstance(poll,bool) or not isinstance(poll,(int,float)) or
                not math.isfinite(poll) or not .1 <= poll <= 60): raise ValueError()
        if interval < poll:
            raise ValueError('history interval must be at least the polling interval')
        previous = data.get('history_interval_seconds',30)
        if (isinstance(previous,bool) or not isinstance(previous,(int,float)) or
                not math.isfinite(previous) or not 1 <= previous <= 3600): raise ValueError()
        # Locate root-level values with the JSON decoder, so identically named
        # nested keys and strings cannot be changed by a textual replacement.
        decoder = json.JSONDecoder()
        def skip(index):
            while index < len(text) and text[index].isspace(): index += 1
            return index
        index = skip(skip(0)+1)
        matches,last_end = [],None
        while text[index] != '}':
            key,index = decoder.raw_decode(text,index)
            index = skip(index)
            if text[index] != ':': raise ValueError()
            start = skip(index+1)
            _,end = decoder.raw_decode(text,start)
            if key == 'history_interval_seconds': matches.append((start,end))
            last_end = end
            index = skip(end)
            if text[index] == '}': break
            if text[index] != ',': raise ValueError()
            index = skip(index+1)
        if len(matches) > 1: raise ValueError()
        if matches:
            start,end = matches[0]
            changed = text[:start]+str(interval)+text[end:]
        else:
            insert = last_end if last_end is not None else index
            changed = text[:insert]+(', ' if last_end is not None else '')+'"history_interval_seconds": '+str(interval)+text[insert:]
        updated = (b'\xef\xbb\xbf' if original.startswith(b'\xef\xbb\xbf') else b'')+changed.encode('utf-8')
        expected = dict(data,history_interval_seconds=interval)
        if json.loads(updated.decode('utf-8-sig')) != expected: raise ValueError()
        return dict(path=path,original=original,updated=updated,interval_seconds=interval)
    except (OSError,UnicodeError,ValueError,TypeError,IndexError):
        raise ValueError(f'Local collector history configuration is missing or invalid; {interval} seconds must be at least the polling interval; nothing was changed') from None


def replace_configuration(path,data):
    temporary = path.with_name(path.name+'.source-update.tmp')
    try:
        with temporary.open('wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def verify(package,root,manifest):
    if not (root/'offline-install.json').is_file() or not (root/'Start.ps1').is_file():
        raise ValueError('Unrecognized installation; nothing was changed')
    profile = manifest.get('profile')
    if profile not in (None,'query-only-hotfix'):
        raise ValueError('Unknown update profile')
    snapshot_check_policy(manifest)
    history_configuration_patch(root,manifest)
    if profile == 'query-only-hotfix' and not set(manifest['files']).issubset({
            'backend/app/decision_hourly/ash_history.py','backend/app/decision_hourly/ash_settlement.py',
            'backend/app/decision_hourly/quality_advice.py','backend/app/routers/health.py',
            'backend/app/gpt_coarse/amount.py','backend/app/routers/decision.py',
            'frontend/js/decision-overview.js','frontend/js/decision-sources.js',
            'frontend/js/api.js','frontend/js/operations.js'}):
        raise ValueError('Query hotfix cannot change schema, model, storage or startup code')
    for name,info in manifest['files'].items():
        payload,target = safe_path(package/'payload',name),safe_path(root,name)
        if not payload.is_file() or digest(payload) != info['sha256']:
            raise ValueError('Incomplete or damaged update: '+name)
        accepted = info.get('accepted_previous_sha256',[])
        if not isinstance(accepted,list) or any(not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{64}',value) for value in accepted):
            raise ValueError('Invalid accepted source hashes: '+name)
        if target.exists():
            actual = digest(target)
            if actual not in {info['sha256'],info.get('previous_sha256'),*accepted}:
                raise ValueError('Installed source differs from update baseline: '+name+' (sha256='+actual+')')
        if not target.exists() and info.get('previous_sha256'):
            raise ValueError('Installed source is missing: '+name)
    if collector_changed(manifest) and not (package/'StopOwnedCollector.ps1').is_file():
        raise ValueError('Collector update requires StopOwnedCollector.ps1; nothing was changed')


def collector_configuration(root):
    """Read only the non-secret fields needed for a bounded restart."""
    try:
        data = json.loads((root/'backend/config/kepserver.local.json').read_text(encoding='utf-8-sig'))
        enabled,source_id = data.get('enabled',False),data.get('source_id','kepserver-main')
        poll,timeout = float(data.get('poll_seconds',5)),float(data.get('timeout_seconds',10))
        if (not isinstance(enabled,bool) or not isinstance(source_id,str) or
                not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}',source_id) or
                not math.isfinite(poll) or not .1 <= poll <= 60 or
                not math.isfinite(timeout) or not .1 <= timeout <= 30):
            raise ValueError()
        return dict(enabled=enabled,source_id=source_id,
            # 2026-10-22：现场 KEP 会反复停转数分钟（当日 08:29/11:05/11:16 三次）。
            # 旧窗口 max(90, poll*3+timeout*3+5)=95s 只覆盖正常重启；重启恰逢停转时
            # 新采集器（按瞬态重试）在窗口内到不了 connected，被误判失败回滚——
            # 而回滚后的旧代码同样超窗（本次 11:06 判败、11:07:30 实测已连上）。
            # 窗口必须覆盖 retry_max(30s) 指数退避的数个周期，放宽到 300s。
            ready_timeout=max(300,poll*3+timeout*3+5))
    except (OSError,ValueError,TypeError,AttributeError):
        raise ValueError('Local collector configuration is missing or invalid; nothing was changed') from None


def collector_status(root,source_id):
    """Never acquire or clear a lease, even during recovery."""
    path = root/'backend/data/dense_medium.db'
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)) as database:
        database.row_factory = sqlite3.Row
        if not database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='telemetry_sources'").fetchone():
            return {}
        source = database.execute('SELECT owner,lease_until,state,last_sample_at FROM telemetry_sources WHERE source_id=?',(source_id,)).fetchone()
        return dict(source) if source else {}


def utc_timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None: raise ValueError('Collector timestamp lacks a timezone')
    return parsed.astimezone(timezone.utc)


def wait_collector_lease(root,configuration,report,*,timeout=300):
    started,last_report = time.monotonic(),-float('inf')
    while True:
        now = time.monotonic()
        if now-started >= timeout:
            raise TimeoutError('Previous collector lease did not expire; its database owner was not changed')
        try:
            status = collector_status(root,configuration['source_id'])
        except sqlite3.OperationalError:
            status = None
        if status is not None:
            lease = status.get('lease_until')
            remaining = max(0,(utc_timestamp(lease)-datetime.now(timezone.utc)).total_seconds()) if lease and status.get('owner') else 0
            if remaining == 0:
                report('copying','Previous collector lease has expired; production database owner was not changed.')
                return
            message = f'Waiting for the previous collector lease to expire naturally ({remaining:.0f}s remaining, {now-started:.0f}s elapsed).'
        else:
            message = f'Waiting to read the previous collector lease ({now-started:.0f}s elapsed).'
        if now-last_report >= 5:
            report('copying',message)
            last_report = now
        time.sleep(min(1,max(0,timeout-(time.monotonic()-started))))


def collector_ready(root,configuration,before,report,*,timeout=None):
    if not configuration['enabled']:
        report('starting','Collector configuration is disabled; collector readiness check skipped.')
        return
    started,last_report = time.monotonic(),-float('inf')
    timeout = configuration['ready_timeout'] if timeout is None else timeout
    not_before = datetime.now(timezone.utc)
    previous = utc_timestamp(before['last_sample_at']) if before.get('last_sample_at') else None
    observed,observed_owner = None,None
    while time.monotonic()-started < timeout:
        now = time.monotonic()
        try:
            status = collector_status(root,configuration['source_id'])
        except sqlite3.OperationalError:
            status = {}
        owner,sample,lease = status.get('owner'),status.get('last_sample_at'),status.get('lease_until')
        if (status.get('state') == 'connected' and owner and owner != before.get('owner') and
                sample and lease and utc_timestamp(lease) > datetime.now(timezone.utc)):
            sampled_at = utc_timestamp(sample)
            # Replayed spool batches may advance last_sample_at too. Require
            # observations captured after this verification began, then growth.
            if sampled_at > not_before and (previous is None or sampled_at > previous):
                if observed_owner != owner:
                    observed,observed_owner = None,owner
                if observed is not None and sampled_at > observed:
                    report('starting','Collector connected with a new owner; two committed sample times advanced after restart (latest '+sample+').')
                    return
                observed = sampled_at if observed is None else observed
        if now-last_report >= 5:
            report('starting',f'Waiting for the restarted collector to connect and advance samples ({now-started:.0f}s elapsed).')
            last_report = now
        time.sleep(min(1,max(0,timeout-(time.monotonic()-started))))
    raise TimeoutError('Restarted collector did not confirm connected state and advancing samples')


# Snapshot verification is staged by size. A full quick_check of a multi-GiB
# production snapshot on a slow host both exceeds any sane fixed budget and
# starves the still-running old collector of disk I/O (observed 2026-10-21:
# 6.25 GiB backup copied in 493s, then the check saturated the disk, the old
# collector hit its 15s write timeout and dropped OPC). Larger snapshots are
# structurally verified before any service is touched, and their full content
# check runs only after the updated collector (spool-backed, contention
# tolerant) is live.
FULL_CHECK_MAX_BYTES = 1024**3
MANUAL_CHECK_MIN_BYTES = 64 * 1024**2
DEFERRED_CHECK_BASE_SECONDS = 300
DEFERRED_CHECK_SECONDS_PER_BYTE = 6e-7   # ~600s per GiB observed-worst allowance
DEFERRED_CHECK_CACHE_KIB = 262144        # 256 MiB decode cache for the post-update scan
DEFERRED_CHECK_YIELD_SECONDS = 0.05      # 50ms sleep each second: keeps page queries responsive


def snapshot_check_policy(manifest):
    """Only an explicit standard update can defer large snapshot content scans."""
    if 'snapshot_content_check_policy' not in manifest:
        return None
    policy = manifest['snapshot_content_check_policy']
    if policy != 'maintenance-window' or manifest.get('profile') is not None:
        raise ValueError('Unknown or incompatible snapshot content check policy')
    return policy


def structure_fingerprint(db):
    """Cheap structural identity: page counts plus the schema bytes."""
    digest = hashlib.sha256()
    for row in db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY rowid').fetchall():
        for value in row:
            digest.update((value if value is not None else '\0').encode('utf-8','surrogatepass'))
            digest.update(b'\x1f')
        digest.update(b'\x1e')
    return (db.execute('PRAGMA page_count').fetchone()[0],
            db.execute('PRAGMA freelist_count').fetchone()[0],digest.hexdigest())


def verify_snapshot_content(path,report=lambda stage,message:None,*,budget):
    """Full quick_check on the finished snapshot; bounded and interruptible.

    The scan yields briefly every second: an unthrottled random-read stream
    saturates the host disk for the whole check and pushes page queries past
    their 30s timeouts (observed 2026-10-21: HTTP 500s for ~40 minutes while
    production sampling stayed healthy). Sampling stays valid either way; the
    throttle keeps the *interactive* pages responsive during the scan."""
    started = time.monotonic()
    last_report = started
    last_yield = started
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as database:
        database.execute(f'PRAGMA cache_size=-{DEFERRED_CHECK_CACHE_KIB}')
        def content_progress():
            nonlocal last_report,last_yield
            now = time.monotonic()
            if now-started >= budget: return 1
            if now-last_yield >= 1:
                time.sleep(DEFERRED_CHECK_YIELD_SECONDS)
                last_yield = time.monotonic()
            if now-last_report >= 30:
                report('verifying',f'Full snapshot content check running, {now-started:.0f}s elapsed.')
                last_report = now
            return 0
        database.set_progress_handler(content_progress,10000)
        try:
            result = database.execute('PRAGMA quick_check').fetchone()
        except sqlite3.OperationalError:
            if time.monotonic()-started >= budget:
                report('verifying','Snapshot content check exceeded its time budget; snapshot retained for manual verification.')
                return False
            raise
        finally:
            database.set_progress_handler(None,0)
        if result is None or result[0] != 'ok':
            report('verifying','Snapshot content check reported corruption; production database was untouched by the update.')
            return False
    report('verifying',f'Snapshot full content check passed ({time.monotonic()-started:.0f}s).')
    return True


def backup_database(root,backup,report=lambda stage,message:None,*,timeout=1200,
                    defer_large_snapshot_check=False):
    if type(defer_large_snapshot_check) is not bool:
        raise ValueError('Deferred snapshot verification requires an explicit boolean')
    path = root/'backend/data/dense_medium.db'
    started = time.monotonic()
    last_report = started-5
    report('copying','Copying production database; collection remains running.')
    def progress(status,remaining,total):
        nonlocal last_report
        now = time.monotonic()
        if now-started >= timeout:
            raise TimeoutError('Database backup timed out; source files and running service were not changed.')
        if now-last_report >= 5 or remaining == 0:
            percent = (total-remaining)*100/total if total else 0
            report('copying',f'Database backup {percent:.1f}% ({total-remaining}/{total} pages), {now-started:.0f}s elapsed.')
            last_report = time.monotonic()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as source:
        # Hold one WAL read snapshot so collection cannot restart a batched copy.
        source.execute('BEGIN')
        source.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
        with closing(sqlite3.connect(backup/'dense_medium.db')) as target:
            try:
                source.backup(target,pages=2048,progress=progress,sleep=.1)
                # Read from the pinned snapshot so the fingerprint describes the
                # exact bytes that were copied, not a later state.
                copied = structure_fingerprint(source)
            finally:
                source.rollback()
            size = (backup/'dense_medium.db').stat().st_size
            full_check_limit = MANUAL_CHECK_MIN_BYTES if defer_large_snapshot_check else FULL_CHECK_MAX_BYTES
            if size <= full_check_limit:
                report('copying','Database copied; checking backup integrity. Collection remains running.')
                check_started = time.monotonic()
                last_check_report = check_started
                def check_progress():
                    nonlocal last_check_report
                    now = time.monotonic()
                    if now-check_started >= timeout: return 1
                    if now-last_check_report >= 5:
                        report('copying',f'Checking database backup integrity, {now-check_started:.0f}s elapsed.')
                        last_check_report = time.monotonic()
                    return 0
                target.set_progress_handler(check_progress,10000)
                try:
                    if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError('Database backup integrity check failed')
                except sqlite3.OperationalError as exc:
                    if time.monotonic()-check_started >= timeout:
                        raise TimeoutError('Backup integrity check timed out; source files and running service were not changed.') from exc
                    raise
                finally:
                    target.set_progress_handler(None,0)
                report('copying','Database backup verified. Preparing source update.')
                return dict(content_check='full',**dict(zip(('page_count','freelist_count','schema_sha256'),copied)))
            if structure_fingerprint(target) != copied:
                raise ValueError('Database backup structural check failed; source files and running service were not changed.')
            report('copying',f'Database copied ({size/1048576:.0f} MiB); structural check passed. '+
                ('Full content verification is deferred to a maintenance window; this recovery copy remains protected and unverified.'
                 if defer_large_snapshot_check else
                 'Full content check runs after the updated services are live, so this scan cannot starve the running collector.'))
            return dict(content_check='deferred',**dict(zip(('page_count','freelist_count','schema_sha256'),copied)))


def powershell(script,*args,cwd):
    subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),*args],
                   cwd=cwd,check=True,timeout=120)


def clear_bytecode(path):
    if path.suffix == '.py':
        Path(importlib.util.cache_from_source(str(path))).unlink(missing_ok=True)


def restart(root):
    powershell(root/'Start.ps1','-NoBrowser',cwd=root)
    for _ in range(30):
        try:
            with urlopen('http://127.0.0.1:8000/api/v1/health',timeout=3) as response:
                if json.load(response)['status'] == 'ok': return
        except (OSError,ValueError): pass
        time.sleep(1)
    raise RuntimeError('Updated service did not become healthy')


def register_retention_snapshot(root,backup,snapshot,deferred_check,created_at,report=lambda *_:None):
    """Reuse completed checks; registry certification never reopens the DB.

    Source-only older installations without the new module remain protected
    legacy backups. A failed/timed-out deferred content check is never promoted
    to verified just because startup or the structural check succeeded.
    """
    if snapshot is None:
        return
    module_path = root/'backend/app/storage_retention/files.py'
    if not module_path.is_file():
        report('verifying','Retention certification unavailable; snapshot remains protected as legacy.')
        return
    try:
        spec = importlib.util.spec_from_file_location('dmcs_update_retention_files',module_path)
        retention = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(retention)
        storage_root = retention.backup_registry_root(root,root/'backend/data/dense_medium.db')
        identity = retention.installation_identity(root)
        backup_id = 'source-update:'+backup.name
        arguments = dict(installation_id=identity,backup_id=backup_id,created_at=created_at)
        full_passed = (snapshot.get('content_check') == 'full' or
            snapshot.get('content_check') == 'deferred' and (deferred_check or {}).get('passed') is True)
        if full_passed:
            retention.register_verified_backup(storage_root,backup.resolve(strict=True),**arguments,
                verification=dict(method='sqlite-full-check',result='ok',checked_at=datetime.now(timezone.utc)))
            retention.mark_backup_runtime_verified(storage_root,backup_id,installation_id=identity,
                verified_at=datetime.now(timezone.utc),select_stable_if_ready=True)
            report('verifying','Completed snapshot checks registered; certified recovery points remain protected by retention policy.')
        else:
            retention.register_unverified_backup(storage_root,backup.resolve(strict=True),**arguments,
                reason='Full content check was deferred, failed or exceeded its budget; manual verification is required.')
            report('verifying','Snapshot remains protected/unverified; it cannot replace a verified stable recovery point.')
    except (OSError, ValueError, ImportError, AttributeError) as exc:
        # A bookkeeping problem cannot roll back successfully installed code or
        # turn an unknown recovery copy into a deletion candidate.
        report('verifying','Snapshot retained; retention certification unavailable: '+type(exc).__name__)


def apply(package,root,manifest,report=lambda stage,message:None):
    verify(package,root,manifest)
    snapshot_policy = snapshot_check_policy(manifest)
    history_patch = history_configuration_patch(root,manifest)
    cycle_collector = collector_changed(manifest)
    configuration = collector_configuration(root) if cycle_collector else None
    backup = root/'backups'/('source-update-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup_created_at = datetime.now(timezone.utc)
    backup.mkdir(parents=True)
    snapshot = None
    # Snapshot the live WAL database consistently; the collector can keep running.
    if manifest.get('profile') == 'query-only-hotfix':
        # This allowlisted update cannot alter schema or model/data files.
        # Keep the previously verified full host backup and save old source below.
        report('copying','Query-only hotfix: saving source backup; production database and verified full backup are retained.')
    else:
        snapshot = (backup_database(root,backup,report,defer_large_snapshot_check=True)
                    if snapshot_policy == 'maintenance-window' else backup_database(root,backup,report))
    if history_patch is not None:
        saved_configuration = backup/'configuration/backend/config/kepserver.local.json'
        saved_configuration.parent.mkdir(parents=True,exist_ok=True)
        saved_configuration.write_bytes(history_patch['original'])
    original = {}
    for name in manifest['files']:
        source = safe_path(root,name)
        original[name] = source.is_file()
        if source.is_file():
            saved = backup/'source'/name
            saved.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,saved)
    (backup/'files.json').write_text(json.dumps(original,indent=2),encoding='utf-8')
    report('copying','Backup complete; preparing the project service update.')
    before = collector_status(root,configuration['source_id']) if cycle_collector and configuration['enabled'] else {}
    services_stopped = False
    configuration_changed = False
    try:
        # Enter recovery before stopping either process. A failed stop or expired
        # wait must still restore the previous sources and bring Web back up.
        services_stopped = True
        powershell(package/'StopOwnedWeb.ps1','-InstallDir',str(root),cwd=root)
        if cycle_collector:
            report('copying','Stopping only this installation\'s collector; spool files and SQLite transactions are retained.')
            powershell(package/'StopOwnedCollector.ps1','-InstallDir',str(root),cwd=root)
            if configuration['enabled']: wait_collector_lease(root,configuration,report)
        if history_patch is not None:
            if history_patch['path'].read_bytes() != history_patch['original']:
                raise ValueError('Collector configuration changed during backup; the history interval was not changed')
            # Recovery must also cover interruption immediately after replace.
            configuration_changed = True
            replace_configuration(history_patch['path'],history_patch['updated'])
            report('copying',f'Collector history storage interval set to {history_patch["interval_seconds"]} seconds; polling and all other settings preserved.')
        report('copying','Installing verified source files.')
        for name in manifest['files']:
            target = safe_path(root,name)
            target.parent.mkdir(parents=True,exist_ok=True)
            temporary = target.with_name(target.name+'.source-update.tmp')
            shutil.copy2(safe_path(package/'payload',name),temporary)
            temporary.replace(target)
            clear_bytecode(target)
        report('starting','Starting updated services and checking health.')
        restart(root)
        if cycle_collector: collector_ready(root,configuration,before,report)
        with urlopen('http://127.0.0.1:8000/api/v1/decision/hourly',timeout=45) as response:
            data = json.load(response)
            if data.get('advice_policy') != 'gpt-hourly-reference-v4' and data.get('status') != 'pending':
                raise RuntimeError('Updated hourly policy was not loaded')
        with urlopen('http://127.0.0.1:8000/',timeout=15) as response:
                if '实时总精煤灰分' not in response.read().decode('utf-8'):
                    raise RuntimeError('Updated frontend was not loaded')
        deferred_check = None
        if snapshot and snapshot.get('content_check') == 'deferred':
            # Run the full content check only now: the updated collector (spool
            # backed, contention tolerant) is live, so this scan can no longer
            # starve production sampling. The budget scales with snapshot size.
            snapshot_bytes = (backup/'dense_medium.db').stat().st_size
            if snapshot_policy == 'maintenance-window' and snapshot_bytes > MANUAL_CHECK_MIN_BYTES:
                snapshot = dict(snapshot,content_check='deferred_manual')
                deferred_check = dict(budget_seconds=0,passed=False,reason='maintenance-window',
                                      snapshot_bytes=snapshot_bytes)
                report('verifying',f'Snapshot content scan deferred to a maintenance window ({snapshot_bytes/1048576:.0f} MiB). '
                    'Structural check passed; this backup remains unverified and cannot replace certified recovery points.')
            else:
                budget = DEFERRED_CHECK_BASE_SECONDS + int(snapshot_bytes*DEFERRED_CHECK_SECONDS_PER_BYTE)
                report('verifying',f'Running deferred full snapshot content check ({snapshot_bytes/1048576:.0f} MiB, budget {budget}s).')
                deferred_check = dict(budget_seconds=budget,
                    passed=verify_snapshot_content(backup/'dense_medium.db',report,budget=budget))
    except BaseException as failure:
        report('copying','Update failed; restoring previous source and restarting services. Production database will be retained.')
        recovery_before = {}
        recovery_error = None
        if services_stopped:
            try:
                powershell(package/'StopOwnedWeb.ps1','-InstallDir',str(root),cwd=root)
                if cycle_collector:
                    if configuration['enabled']: recovery_before = collector_status(root,configuration['source_id'])
                    powershell(package/'StopOwnedCollector.ps1','-InstallDir',str(root),cwd=root)
            except Exception as exc:
                recovery_error = exc
        if configuration_changed:
            replace_configuration(history_patch['path'],history_patch['original'])
        for name,existed in original.items():
            target = safe_path(root,name)
            if existed: shutil.copy2(backup/'source'/name,target)
            else: target.unlink(missing_ok=True)
            clear_bytecode(target)
        if cycle_collector and configuration['enabled'] and recovery_error is None:
            try: wait_collector_lease(root,configuration,report)
            except Exception as exc: recovery_error = exc
        # Even a lease which exceeds the bounded wait must not leave Web down.
        # Start.ps1 respects an existing lease; no owner is forcibly reclaimed.
        try:
            restart(root)
            if cycle_collector and recovery_error is None:
                collector_ready(root,configuration,recovery_before,report)
        except Exception as exc:
            recovery_error = exc
        if recovery_error is not None:
            raise RuntimeError('Update failed; previous source was restored, but service recovery could not be verified: '+str(recovery_error)) from failure
        raise
    (root/'source-update.json').write_text(json.dumps(dict(revision=manifest['revision'],
        installed_at=datetime.now().astimezone().isoformat(),backup=str(backup),
        profile=manifest.get('profile') or 'standard',
        database_snapshot=manifest.get('profile') != 'query-only-hotfix',
        snapshot_verification=(snapshot or {}).get('content_check'),
        snapshot_content_check=deferred_check,
        **({'snapshot_content_check_policy':snapshot_policy} if snapshot_policy is not None else {}),
        collector_restarted=cycle_collector and configuration['enabled'],
        **({'collector_history_interval_seconds':history_patch['interval_seconds']} if history_patch is not None else {})),indent=2),encoding='utf-8')
    register_retention_snapshot(root,backup,snapshot,deferred_check,backup_created_at,report)
    return backup


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--install-dir',type=Path,required=True)
    args = parser.parse_args()
    package = Path(__file__).resolve().parent
    manifest = json.loads((package/'update.json').read_text(encoding='utf-8'))
    def report(stage,message):
        print(stage+': '+message,flush=True)
        try:
            data = json.dumps(dict(stage=stage,message=message[:1000],revision=manifest['revision'],
                healthy=stage=='installed',at=datetime.now().astimezone().isoformat())).encode()
            with urlopen(Request(manifest['report_url'],data=data,headers={'Content-Type':'application/json'}),timeout=4): pass
        except OSError: pass
    report('verifying',
        f'Verifying source update and explicit {manifest["collector_history_interval_seconds"]}-second history setting; production database and all other configuration will be preserved.'
        if 'collector_history_interval_seconds' in manifest else
        'Verifying source update; production database/configuration will be preserved.')
    try:
        backup = apply(package,args.install_dir.resolve(),manifest,report)
        report('installed','Source update complete. Backup: '+str(backup)+'. Refresh Chrome with Ctrl+F5.')
        return 0
    except Exception as exc:
        report('failed',str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
