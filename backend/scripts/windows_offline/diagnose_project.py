"""Read-only bounded host diagnostics. Never starts/stops services or writes SQLite."""
import argparse
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
from urllib.request import Request, urlopen


def log_tail(path):
    if not path.is_file(): return 'not found'
    with path.open('rb') as stream:
        stream.seek(max(0,path.stat().st_size-32768))
        lines = stream.read().decode('utf-8-sig',errors='replace').splitlines()
    selected = [line for line in lines if re.search(
        r'error|traceback|exception|failed|locked|queuepool|timeout|collector_|startup|shutdown',line,re.I)]
    text = '\n'.join((selected or lines)[-45:])[-8000:]
    return re.sub(r'([?&]token=)[^&\s"]+',r'\1[redacted]',text,flags=re.I)


def collect(root):
    report = dict(at=datetime.now().astimezone().isoformat())
    usage = shutil.disk_usage(root)
    report['disk_free_mb'] = round(usage.free/1048576)
    report['files'] = {}
    for suffix in ('','-wal','-shm'):
        path = root/('backend/data/dense_medium.db'+suffix)
        if path.exists():
            report['files'][path.name] = dict(bytes=path.stat().st_size,modified=path.stat().st_mtime)
    root_literal = str(root).replace("'","''")
    command = ("$ErrorActionPreference='Stop';$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
        "$p=@(Get-CimInstance Win32_Process | Where-Object {$_.Name -match '^python(w)?\\.exe$' -and "
        "$_.CommandLine -like '*"+root_literal+"*'} | Select-Object ProcessId,ParentProcessId,WorkingSetSize,"
        "KernelModeTime,UserModeTime,ReadTransferCount,WriteTransferCount);"
        "$m=Get-CimInstance Win32_OperatingSystem | Select-Object FreePhysicalMemory,TotalVisibleMemorySize;"
        "@{processes=$p;memory=$m}|ConvertTo-Json -Depth 4 -Compress")
    try:
        encoded = base64.b64encode(command.encode('utf-16-le')).decode()
        result = subprocess.run(['powershell.exe','-NoProfile','-EncodedCommand',encoded],
            capture_output=True,timeout=15)
        report['processes'] = json.loads(result.stdout.decode('utf-8-sig')) if result.returncode == 0 else 'query failed'
    except (OSError,ValueError,subprocess.TimeoutExpired): report['processes'] = 'query unavailable'
    report['http'] = {}
    for endpoint in ('/api/v1/health/live','/api/v1/health/ready'):
        started = time.monotonic()
        try:
            with urlopen('http://127.0.0.1:8000'+endpoint,timeout=3) as response:
                report['http'][endpoint] = dict(status=response.status,elapsed=round(time.monotonic()-started,2),body=json.load(response))
        except Exception as exc:
            report['http'][endpoint] = dict(error=type(exc).__name__,elapsed=round(time.monotonic()-started,2))
    started = time.monotonic()
    try:
        path = root/'backend/data/dense_medium.db'
        db = sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)
        try:
            db.execute('PRAGMA query_only=ON')
            db.set_progress_handler(lambda:int(time.monotonic()-started>3),1000)
            report['database'] = dict(tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")],
                                      elapsed=round(time.monotonic()-started,2))
        finally: db.close()
    except Exception as exc: report['database'] = dict(error=type(exc).__name__,message=str(exc)[:250])
    report['logs'] = {'web':log_tail(Path(os.environ.get('TEMP','.'))/'dmcs_server.log')}
    runtime = root/'backend/data/telemetry-runtime'
    for metadata in sorted(runtime.glob('*.json'))[:2]:
        try:
            data = json.loads(metadata.read_text(encoding='utf-8-sig'))
            for name in ('stdout','stderr'):
                path = Path(data.get(name,'')).resolve()
                if path.is_relative_to(runtime.resolve()): report['logs']['collector_'+name] = log_tail(path)
        except (OSError,ValueError,TypeError): pass
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--install-dir',type=Path,required=True)
    args = parser.parse_args()
    root = args.install_dir.resolve()
    if not (root/'offline-install.json').is_file(): raise SystemExit('Unrecognized installation')
    print('Collecting read-only diagnostics (no service restart, no database modification)...',flush=True)
    report = collect(root)
    target = Path(__file__).with_name('diagnostics.json')
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    try:
        config = json.loads(Path(__file__).with_name('diagnostic-config.json').read_text(encoding='utf-8'))
        data = json.dumps(report,ensure_ascii=False).encode()
        with urlopen(Request(config['report_url'],data=data,headers={'Content-Type':'application/json'}),timeout=5): pass
        print('Diagnostics delivered. Saved locally: '+str(target),flush=True)
    except Exception as exc: print('Report saved locally; delivery failed: '+type(exc).__name__,flush=True)


if __name__ == '__main__': main()
