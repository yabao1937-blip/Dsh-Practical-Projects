"""Verified source-only update. Never replace production databases/config/models."""
import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
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


def verify(package,root,manifest):
    if not (root/'offline-install.json').is_file() or not (root/'Start.ps1').is_file():
        raise ValueError('Unrecognized installation; nothing was changed')
    for name,info in manifest['files'].items():
        payload,target = safe_path(package/'payload',name),safe_path(root,name)
        if not payload.is_file() or digest(payload) != info['sha256']:
            raise ValueError('Incomplete or damaged update: '+name)
        if target.exists() and digest(target) not in {info['sha256'],info.get('previous_sha256')}:
            raise ValueError('Installed source differs from update baseline: '+name)
        if not target.exists() and info.get('previous_sha256'):
            raise ValueError('Installed source is missing: '+name)


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
    backup = root/'backups'/('source-update-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir(parents=True)
    # Snapshot the live WAL database consistently; the collector can keep running.
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
    report('copying','Backup complete; restarting only the project Web service.')
    powershell(package/'StopOwnedWeb.ps1','-InstallDir',str(root),cwd=root)
    try:
        report('copying','Installing verified source files.')
        for name in manifest['files']:
            target = safe_path(root,name)
            target.parent.mkdir(parents=True,exist_ok=True)
            temporary = target.with_name(target.name+'.source-update.tmp')
            shutil.copy2(safe_path(package/'payload',name),temporary)
            temporary.replace(target)
            clear_bytecode(target)
        report('starting','Starting updated Web service and checking health.')
        restart(root)
        with urlopen('http://127.0.0.1:8000/api/v1/decision/hourly',timeout=45) as response:
            data = json.load(response)
            if data.get('advice_policy') != 'gpt-hourly-reference-v4' and data.get('status') != 'pending':
                raise RuntimeError('Updated hourly policy was not loaded')
        with urlopen('http://127.0.0.1:8000/',timeout=15) as response:
            if '实时总精煤灰分' not in response.read().decode('utf-8'):
                raise RuntimeError('Updated frontend was not loaded')
    except BaseException:
        report('copying','Update failed; restoring previous source and restarting Web. Production database will be retained.')
        powershell(package/'StopOwnedWeb.ps1','-InstallDir',str(root),cwd=root)
        for name,existed in original.items():
            target = safe_path(root,name)
            if existed: shutil.copy2(backup/'source'/name,target)
            else: target.unlink(missing_ok=True)
            clear_bytecode(target)
        restart(root)
        raise
    (root/'source-update.json').write_text(json.dumps(dict(revision=manifest['revision'],
        installed_at=datetime.now().astimezone().isoformat(),backup=str(backup)),indent=2),encoding='utf-8')
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
