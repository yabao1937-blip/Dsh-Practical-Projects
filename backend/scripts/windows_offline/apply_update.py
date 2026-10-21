"""Verified source-only update. Never replace production databases/config/models."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
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
    return manifest.get('profile') != 'query-only-hotfix' and any(
        name.startswith(('backend/app/telemetry/','backend/migrations/')) or name in {
            'backend/collector.py','backend/client_mode.py','backend/scripts/start_collector.py',
            'backend/app/database.py','backend/app/config.py','backend/app/models.py',
            'backend/app/schema_upgrade.py','backend/app/decision_hourly/models.py'}
        for name in manifest['files'])


def verify(package,root,manifest):
    if not (root/'offline-install.json').is_file() or not (root/'Start.ps1').is_file():
        raise ValueError('Unrecognized installation; nothing was changed')
    profile = manifest.get('profile')
    if profile not in (None,'query-only-hotfix'):
        raise ValueError('Unknown update profile')
    if profile == 'query-only-hotfix' and not set(manifest['files']).issubset({
            'backend/app/decision_hourly/ash_history.py','backend/app/decision_hourly/ash_settlement.py',
            'backend/app/decision_hourly/quality_advice.py','backend/app/routers/health.py',
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
            ready_timeout=max(90,poll*3+timeout*3+5))
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


def backup_database(root,backup,report=lambda stage,message:None,*,timeout=1200):
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
            finally:
                source.rollback()
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


def apply(package,root,manifest,report=lambda stage,message:None):
    verify(package,root,manifest)
    cycle_collector = collector_changed(manifest)
    configuration = collector_configuration(root) if cycle_collector else None
    backup = root/'backups'/('source-update-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir(parents=True)
    # Snapshot the live WAL database consistently; the collector can keep running.
    if manifest.get('profile') == 'query-only-hotfix':
        # This allowlisted update cannot alter schema or model/data files.
        # Keep the previously verified full host backup and save old source below.
        report('copying','Query-only hotfix: saving source backup; production database and verified full backup are retained.')
    else:
        backup_database(root,backup,report)
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
    try:
        # Enter recovery before stopping either process. A failed stop or expired
        # wait must still restore the previous sources and bring Web back up.
        services_stopped = True
        powershell(package/'StopOwnedWeb.ps1','-InstallDir',str(root),cwd=root)
        if cycle_collector:
            report('copying','Stopping only this installation\'s collector; spool files and SQLite transactions are retained.')
            powershell(package/'StopOwnedCollector.ps1','-InstallDir',str(root),cwd=root)
            if configuration['enabled']: wait_collector_lease(root,configuration,report)
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
        collector_restarted=cycle_collector and configuration['enabled']),indent=2),encoding='utf-8')
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
    report('verifying','Verifying source update; production database/configuration will be preserved.')
    try:
        backup = apply(package,args.install_dir.resolve(),manifest,report)
        report('installed','Source update complete. Backup: '+str(backup)+'. Refresh Chrome with Ctrl+F5.')
        return 0
    except Exception as exc:
        report('failed',str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
