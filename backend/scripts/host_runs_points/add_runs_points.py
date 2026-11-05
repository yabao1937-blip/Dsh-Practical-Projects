"""Append two read-only RUNS diagnostics; keep if_prod and all model bindings."""
import argparse
from contextlib import closing, contextmanager
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

SOURCE_ID = 'kepserver-main'
BUNDLE_FILES = frozenset({'add_runs_points.py','AddRunsPoints.cmd','points-delta.json',
                         'collector_control.py','StopOwnedCollector.ps1','README.txt'})
OLD_POINTS = {
    'tag_3a82e49b2a019451078b':'ns=2;s=TIA_3_3.TIA_3_3.473/if_prod',
    'tag_caf82182c2980109b2de':'ns=2;s=TIA_3_3.TIA_3_3.474/if_prod',
}
POINTS = [dict(key='tag_'+hashlib.sha256(f'TIA_3_3.TIA_3_3.{number}/RUNS'.encode()).hexdigest()[:20],
    name=f'{number}运行反馈（RUNS）',node_id=f'ns=2;s=TIA_3_3.TIA_3_3.{number}/RUNS',
    device='TIA_3_3',tag_name=f'{number}/RUNS',item_id=f'TIA_3_3.TIA_3_3.{number}/RUNS',
    description=f'原始布尔诊断点；设备含义待现场核实；保留{number}/if_prod，不用于模型绑定',
    unit='',value_type='boolean',scale=1,offset=0,minimum=None,maximum=None,
    max_age_seconds=30,max_source_age_seconds=None) for number in (473,474)]


class RunsUpdateError(RuntimeError):
    pass


def strict_json(raw):
    def unique(pairs):
        result = {}
        for key,value in pairs:
            if key in result: raise RunsUpdateError('JSON contains duplicate keys; nothing was changed')
            result[key] = value
        return result
    return json.loads(raw,object_pairs_hook=unique)


def points_close(text):
    decoder = json.JSONDecoder()
    index = text.index('{')+1
    while True:
        while text[index].isspace(): index += 1
        key,index = decoder.raw_decode(text,index)
        while text[index].isspace(): index += 1
        if text[index] != ':': raise RunsUpdateError('Invalid collector JSON')
        index += 1
        while text[index].isspace(): index += 1
        value,end = decoder.raw_decode(text,index)
        if key == 'points' and isinstance(value,list): return end-1
        index = end
        while text[index].isspace(): index += 1
        if text[index] != ',': raise RunsUpdateError('Collector points array was not found')
        index += 1


def plan(original):
    """Insert only new array elements; never serialize existing definitions."""
    from app.telemetry.config import CollectorConfig
    text = original.decode('utf-8-sig')
    data = strict_json(text)
    cfg = CollectorConfig.model_validate(data)
    if (not cfg.enabled or cfg.source_id != SOURCE_ID or cfg.poll_seconds != 5
            or cfg.history_interval_seconds != 60 or cfg.level_history_interval_seconds != 30):
        raise RunsUpdateError('Expected enabled host collection with 5s polling, 60s history and 30s level policy; configuration was preserved')
    for key,node in OLD_POINTS.items():
        matches = [p for p in data['points'] if p.get('key') == key]
        if (len(matches) != 1 or matches[0].get('node_id') != node
                or matches[0].get('item_id') != node.split(';s=',1)[1]
                or matches[0].get('value_type') != 'boolean'):
            raise RunsUpdateError('Existing if_prod identity differs; nothing was changed')
    additions = []
    for point in POINTS:
        matches = [p for p in data['points'] if any(p.get(k) == point[k] for k in ('key','node_id','item_id'))]
        if matches:
            if len(matches) != 1 or any(matches[0].get(k) != value for k,value in point.items()):
                raise RunsUpdateError('RUNS key, node or definition conflicts; nothing was changed')
        else: additions.append(point)
    if not additions: return original,[]
    close = points_close(text)
    insert = close
    while text[insert-1].isspace(): insert -= 1
    rendered = ',\n'.join('\n'.join('    '+line for line in json.dumps(p,ensure_ascii=False,indent=2).splitlines()) for p in additions)
    extra = (',' if data['points'] else '')+'\n'+rendered+'\n  '
    updated = text[:insert]+extra+text[insert:]
    CollectorConfig.model_validate_json(updated)
    encoded = (b'\xef\xbb\xbf' if original.startswith(b'\xef\xbb\xbf') else b'')+updated.encode('utf-8')
    return encoded,[p['key'] for p in additions]


def atomic_config(path,data):
    temporary = path.with_name(path.name+'.runs-'+str(time.time_ns())+'.tmp')
    with temporary.open('xb') as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary,path)


@contextmanager
def update_lock(root):
    path = root/'backend/data/.runs-points-update.lock'
    with path.open('a+b') as stream:
        stream.seek(0,2)
        if stream.tell() == 0: stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            raise RunsUpdateError('Another RUNS configuration update is running') from None
        try: yield
        finally:
            if os.name == 'nt': stream.seek(0); msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def start_collector(root):
    env = os.environ.copy()
    env['DMCS_DATABASE_URL'] = 'sqlite:///'+(root/'backend/data/dense_medium.db').as_posix()
    env['DMCS_TELEMETRY_SPOOL'] = str(root/'backend/data/telemetry-spool')
    result = subprocess.run([sys.executable,str(root/'backend/scripts/start_collector.py'),
        '--config',str(root/'backend/config/kepserver.local.json')],cwd=root,env=env,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=45)
    if result.returncode: raise RunsUpdateError('Collector launcher failed; private output was not printed')


def verify_samples(root,since,*,timeout=120,decode=None):
    if decode is None:
        from point_metadata import Catalog,DIRECTORY
        decode = Catalog((root/'backend/data').resolve()/DIRECTORY,compact_writes=False).loads
    expected = {**OLD_POINTS,**{p['key']:p['node_id'] for p in POINTS}}
    deadline = time.monotonic()+timeout
    database = (root/'backend/data/dense_medium.db').resolve()
    while time.monotonic() < deadline:
        found = {}
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            for key,node in expected.items():
                row = db.execute('SELECT received_at,sample FROM telemetry_readings INDEXED BY ix_telemetry_point_time '
                    'WHERE source_id=? AND point_key=? AND received_at>=? ORDER BY received_at DESC LIMIT 1',
                    (SOURCE_ID,key,since)).fetchone()
                if row:
                    sample = decode(row[1])
                    status = sample.get('status_code')
                    if (sample.get('quality') != 'good' or sample.get('issue') or type(sample.get('value')) is not bool
                            or type(status) is not int or not 0 <= status <= 0xFFFFFFFF or status & 0xC0000000
                            or sample.get('received_at') != row[0] or sample.get('key',key) != key
                            or sample.get('node_id') != node):
                        raise RunsUpdateError('Fresh RUNS/if_prod sample has a type, quality or node mismatch')
                    found[key] = row[0]
        if len(found) == len(expected): return found
        time.sleep(min(2,max(0,deadline-time.monotonic())))
    raise RunsUpdateError('Timed out waiting for fresh samples of the two new and two preserved points')


def apply(root,package,control,report=lambda *args:None,*,dry_run=False):
    config = root/'backend/config/kepserver.local.json'
    required = [root/'offline-install.json',config,root/'backend/data/dense_medium.db',
                root/'backend/scripts/start_collector.py',package/'StopOwnedCollector.ps1']
    if not all(p.is_file() for p in required): raise RunsUpdateError('Recognized host installation or collector helper is missing')
    if os.environ.get('DMCS_REMOTE_READONLY_CONFIG'):
        raise RunsUpdateError('Read-only client environment: installation is not allowed')
    original = config.read_bytes()
    updated,added = plan(original)
    if dry_run: return dict(status='dry-run',added=added)
    if not added:
        cfg = control.collector_configuration(root)
        current = control.collector_status(root,cfg['source_id'])
        now = datetime.now(timezone.utc)
        since = now-timedelta(seconds=180)
        try:
            active = (cfg['enabled'] and current.get('state') == 'connected' and current.get('owner')
                and datetime.fromisoformat(current['lease_until'].replace('Z','+00:00')) > now
                and datetime.fromisoformat(current['last_sample_at'].replace('Z','+00:00')) >= since)
        except (KeyError,TypeError,ValueError): active = False
        if not active:
            raise RunsUpdateError('RUNS definitions exist, but active collector health was not verified; no service was changed')
        samples = verify_samples(root,since.isoformat(timespec='milliseconds'),timeout=.1)
        return dict(status='unchanged-verified',added=[],fresh_samples=samples)
    with update_lock(root):
        if config.read_bytes() != original: raise RunsUpdateError('Configuration changed before update; nothing was overwritten')
        cfg = control.collector_configuration(root)
        before = control.collector_status(root,cfg['source_id'])
        backup = root/'backups'/('runs-points-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        backup.mkdir(parents=True)
        with (backup/'kepserver.local.json').open('xb') as saved:
            saved.write(original); saved.flush(); os.fsync(saved.fileno())
        changed = False
        try:
            report('copying','Configuration backup complete; stopping only the owned collector. Web, database and ordinary spool are retained.')
            control.powershell(package/'StopOwnedCollector.ps1','-InstallDir',str(root),cwd=root)
            control.wait_collector_lease(root,cfg,report)
            if config.read_bytes() != original: raise RunsUpdateError('Configuration changed during stop; nothing was overwritten')
            changed = True
            atomic_config(config,updated)
            since = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
            report('starting','Starting collector with two added raw Boolean RUNS diagnostics; existing if_prod and bindings are unchanged.')
            start_collector(root)
            control.collector_ready(root,cfg,before,report)
            samples = verify_samples(root,since)
        except BaseException:
            report('copying','Update failed; retaining database/spool and restoring only this configuration change.')
            recovery_before = control.collector_status(root,cfg['source_id'])
            control.powershell(package/'StopOwnedCollector.ps1','-InstallDir',str(root),cwd=root)
            current = config.read_bytes()
            if current not in (original,updated):
                raise RunsUpdateError('Configuration changed externally; automatic overwrite and restart were blocked')
            if changed:
                if current != original: atomic_config(config,original)
            recovery_cfg = control.collector_configuration(root)
            control.wait_collector_lease(root,recovery_cfg,report)
            start_collector(root)
            control.collector_ready(root,recovery_cfg,recovery_before,report)
            raise
        result = dict(status='installed',added=added,configuration_sha256=hashlib.sha256(updated).hexdigest(),
                      original_sha256=hashlib.sha256(original).hexdigest(),fresh_samples=samples)
        (backup/'result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-dir',type=Path,required=True)
    parser.add_argument('--dry-run',action='store_true')
    args = parser.parse_args()
    root,package = args.install_dir.resolve(),Path(__file__).resolve().parent
    manifest = strict_json((package/'bundle.json').read_text(encoding='utf-8'))
    if manifest.get('format') != 1 or manifest.get('purpose') != 'desliming-runs-addition-v1' or set(manifest.get('files',{})) != BUNDLE_FILES:
        raise RunsUpdateError('RUNS package scope or format differs from the reviewed bundle')
    for name,digest in manifest['files'].items():
        if Path(name).name != name or hashlib.sha256((package/name).read_bytes()).hexdigest() != digest:
            raise RunsUpdateError('Incomplete or damaged RUNS package')
    if strict_json((package/'points-delta.json').read_text(encoding='utf-8')) != POINTS:
        raise RunsUpdateError('RUNS delta differs from the reviewed two-point scope')
    sys.path.insert(0,str(root/'backend'))
    spec = importlib.util.spec_from_file_location('runs_collector_control',package/'collector_control.py')
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)
    def report(stage,message):
        print(stage+': '+message,flush=True)
        if args.dry_run: return
        try:
            body = json.dumps(dict(stage=stage,message=message,healthy=stage=='installed',
                revision=manifest['version'],at=datetime.now().astimezone().isoformat())).encode()
            with urlopen(Request(manifest['report_url'],data=body,headers={'Content-Type':'application/json'}),timeout=4): pass
        except OSError: pass
    try:
        report('verifying','Adding two RUNS diagnostics only; raw Boolean type is verified, equipment meaning awaits site comparison.')
        result = apply(root,package,control,report,dry_run=args.dry_run)
        report('verifying' if args.dry_run else 'installed',result['status']+': two RUNS definitions; if_prod, model bindings, 5/60/30-second policies preserved.')
        return 0
    except Exception as exc:
        report('failed',str(exc) if isinstance(exc,RunsUpdateError) else type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
