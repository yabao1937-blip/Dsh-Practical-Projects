"""Build the allowlisted query hotfix from committed Git objects only."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]
FILES = ('backend/app/decision_hourly/ash_history.py','backend/app/decision_hourly/ash_settlement.py',
         'backend/app/decision_hourly/quality_advice.py','backend/app/routers/health.py',
         'backend/app/gpt_coarse/amount.py','backend/app/routers/decision.py',
         'frontend/js/decision-overview.js','frontend/js/decision-sources.js',
         'frontend/js/api.js','frontend/js/operations.js')


def git_bytes(ref,name):
    return subprocess.check_output(['git','show',ref+':'+name],cwd=ROOT)


def build(base,output,installed_package):
    revision = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    settings = json.loads((output/'transfer-settings.json').read_text(encoding='utf-8'))
    latest = json.loads((output/'latest.json').read_text(encoding='utf-8'))
    original = json.loads((Path(latest['stage'])/'manifest.json').read_text(encoding='utf-8'))
    with zipfile.ZipFile(installed_package) as installed:
        deployed = json.loads(installed.read('update.json'))
    if deployed['revision'] != base:
        raise ValueError('Installed package revision does not match the requested baseline')
    files,payload = {},{}
    for name in FILES:
        old,new = git_bytes(base,name),git_bytes(revision,name)
        if old == new: continue
        # Use actual deployed bytes (including Windows line endings), not a
        # reconstituted Git checkout, to validate the host baseline.
        previous = deployed['files'].get(name,{}).get('sha256') or original['files'].get('project/'+name)
        if not previous: raise ValueError('Unknown deployed baseline: '+name)
        files[name] = dict(sha256=hashlib.sha256(new).hexdigest(),previous_sha256=previous)
        payload[name] = new
    if not files: raise ValueError('No committed query hotfix files')
    manifest = dict(format=1,profile='query-only-hotfix',revision=revision,baseline=base,
        created_at=datetime.now().astimezone().isoformat(),files=files,
        report_url=f'http://{settings["bind"]}:{settings["port"]}/{settings["token"]}/status')
    target = output/'extras/ProjectUpdate.zip'
    temporary = target.with_suffix('.tmp')
    with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('update.json',json.dumps(manifest,indent=2).encode())
        for name,data in payload.items(): archive.writestr('payload/'+name,data)
        for name in ('apply_update.py','UpdateProject.cmd','StopOwnedWeb.ps1'):
            archive.writestr(name,git_bytes(revision,'backend/scripts/windows_offline/'+name))
    temporary.replace(target)
    print(json.dumps(dict(package=str(target),revision=revision,files=len(files),bytes=target.stat().st_size)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--installed-package',type=Path,required=True)
    args = parser.parse_args()
    build(args.base,args.output.resolve(),args.installed_package.resolve())
