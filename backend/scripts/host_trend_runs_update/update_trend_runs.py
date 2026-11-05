"""Run two independently verified update transactions in a fixed serial order."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
from urllib.request import Request, urlopen
import uuid


COMPONENTS = (
    ('runs','runs/switch_runs.py','--dry-run'),
    ('frontend','frontend/apply_update.py','--preflight'),
)
BUNDLE_TYPE = 'trend-runs-update-v1'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_bundle(package,root):
    if not (root/'offline-install.json').is_file() or not (root/'Start.ps1').is_file():
        raise ValueError('Unrecognized installation; no component was applied')
    manifest = json.loads((package/'bundle.json').read_text(encoding='utf-8'))
    if manifest.get('format') != 1 or manifest.get('type') != BUNDLE_TYPE or not isinstance(manifest.get('files'),dict):
        raise ValueError('Unknown combined update bundle')
    required = {name for _,name,_ in COMPONENTS} | {'frontend/update.json'}
    if not required.issubset(manifest['files']):
        raise ValueError('Combined update is missing a required verified component')
    for name,expected in manifest['files'].items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name:
            raise ValueError('Unsafe combined update path')
        target = (package/name).resolve()
        if not target.is_relative_to(package.resolve()) or not target.is_file() or digest(target) != expected:
            raise ValueError('Incomplete or damaged combined update: '+name)
    return manifest


def save_status(package,status):
    temporary = package/'combined-update-status.tmp'
    temporary.write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(package/'combined-update-status.json')


def callback(package,status):
    """Use the existing portal callback without logging its private address."""
    try:
        component_manifest = json.loads((package/'frontend/update.json').read_text(encoding='utf-8'))
        stage = status['stage'] if status['stage'] in {'installed','failed'} else 'verifying'
        payload = dict(stage=stage,message=status['message'][:1000],
            healthy=stage=='installed',revision=status['revision'],at=datetime.now().astimezone().isoformat())
        request = Request(component_manifest['report_url'],data=json.dumps(payload).encode(),
            headers={'Content-Type':'application/json'})
        with urlopen(request,timeout=4):
            pass
    except (OSError,ValueError,KeyError):
        pass


def run_component(command,log_path,label):
    with log_path.open('w',encoding='utf-8') as log:
        process = subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            text=True,encoding='utf-8',errors='replace')
        assert process.stdout is not None
        for line in process.stdout:
            print('['+label+'] '+line.rstrip(),flush=True)
            log.write(line)
            log.flush()
        return process.wait()


def run(package,root):
    package,root = package.resolve(),root.resolve()
    manifest = verify_bundle(package,root)
    interpreter = root/'backend/.venv/Scripts/python.exe'
    if not interpreter.is_file():
        raise ValueError('Installed Python runtime was not found; no component was applied')
    status = dict(revision=manifest['revision'],stage='preflight',message='Verifying both components before applying either one.',
        started_at=datetime.now().astimezone().isoformat(),components={
            name:dict(preflight='pending',apply='not_started') for name,_,_ in COMPONENTS})
    save_status(package,status)
    callback(package,status)
    logs = package/'update-logs'
    logs.mkdir(exist_ok=True)

    def failed(message):
        status['stage'],status['message'] = 'failed',message
        save_status(package,status)
        callback(package,status)
        print('UPDATE FAILED: '+message,flush=True)
        print('Component status: '+str(package/'combined-update-status.json'),flush=True)
        return 1

    for name,script,preflight_flag in COMPONENTS:
        print('Preflight: '+name,flush=True)
        try:
            code = run_component([str(interpreter),'-X','utf8',str(package/script),
                '--install-dir',str(root),preflight_flag],logs/(name+'-preflight.log'),name+' preflight')
        except OSError:
            code = 1
        status['components'][name]['preflight'] = 'passed' if code == 0 else 'failed'
        status['components'][name]['preflight_exit_code'] = code
        save_status(package,status)
        if code != 0:
            return failed(name+' preflight failed. No component was applied. Review update-logs/'+name+'-preflight.log.')

    # This lock only prevents two copies of this serial coordinator from
    # applying concurrently. Each component owns its original transaction and
    # repeats its own baseline checks; there is no new global rollback.
    lock = root/'trend-runs-update.lock'
    owner = dict(pid=os.getpid(),token=uuid.uuid4().hex,started_at=status['started_at'])
    try:
        with lock.open('x',encoding='utf-8') as output:
            json.dump(owner,output)
    except FileExistsError:
        return failed('Another combined update owns the installation lock. No component was applied by this run.')
    try:
        for name,script,_ in COMPONENTS:
            status['stage'],status['message'] = 'applying','Applying '+name+' component.'
            status['components'][name]['apply'] = 'running'
            save_status(package,status)
            print('Apply: '+name,flush=True)
            try:
                code = run_component([str(interpreter),'-X','utf8',str(package/script),
                    '--install-dir',str(root),'--component'],logs/(name+'-apply.log'),name)
            except OSError:
                code = 1
            status['components'][name]['apply'] = 'installed' if code == 0 else 'failed'
            status['components'][name]['apply_exit_code'] = code
            save_status(package,status)
            if code != 0:
                installed = [key for key,value in status['components'].items() if value['apply']=='installed']
                completed = ', '.join(installed) if installed else 'none'
                return failed(name+' component failed. Completed components: '+completed+
                    '. Remaining components were not applied. Review component logs and recovery status; the bundle is not installed.')
        status['stage'] = 'installed'
        status['message'] = 'RUNS switch and density trend frontend both completed and verified. Refresh Chrome with Ctrl+F5.'
        status['installed_at'] = datetime.now().astimezone().isoformat()
        save_status(package,status)
        callback(package,status)
        print('UPDATE INSTALLED: '+status['message'],flush=True)
        return 0
    finally:
        try:
            if json.loads(lock.read_text(encoding='utf-8')) == owner:
                lock.unlink()
        except (OSError,ValueError):
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--install-dir',type=Path,required=True)
    args = parser.parse_args()
    try:
        return run(Path(__file__).resolve().parent,args.install_dir)
    except Exception as exc:
        print('UPDATE FAILED: '+str(exc),flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
