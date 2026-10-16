"""Build a source-only delta against a verified installed offline bundle."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def build(output):
    latest = json.loads((output/'latest.json').read_text(encoding='utf-8'))
    baseline = json.loads((Path(latest['stage'])/'manifest.json').read_text(encoding='utf-8'))
    settings = json.loads((output/'transfer-settings.json').read_text(encoding='utf-8'))
    tracked = subprocess.check_output(['git','ls-files'],cwd=ROOT,text=True).splitlines()
    tracked += ['backend/client_mode.py','backend/app/gpt_density/hourly_anchor.py',
                'backend/migrations/versions/c3a610160001_0014_production_assay_time.py']
    from windows_offline.apply_update import allowed
    files,payload = {},{}
    for name in sorted(set(tracked)):
        if not allowed(name): continue
        path = ROOT/name
        if not path.is_file(): continue
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        previous = baseline['files'].get('project/'+name)
        if previous == digest: continue
        files[name] = dict(sha256=digest,previous_sha256=previous)
        payload['payload/'+name] = data
    revision = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    manifest = dict(format=1,revision=revision,created_at=datetime.now().astimezone().isoformat(),
        report_url=f'http://{settings["bind"]}:{settings["port"]}/{settings["token"]}/status',files=files)
    target = output/'extras/ProjectUpdate.zip'
    target.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('update.json',json.dumps(manifest,indent=2).encode('utf-8'))
        for name,data in payload.items(): archive.writestr(name,data)
        for name in ('apply_update.py','UpdateProject.cmd','StopOwnedWeb.ps1'):
            archive.write(ROOT/'backend/scripts/windows_offline'/name,name)
    print(json.dumps(dict(package=str(target),bytes=target.stat().st_size,files=len(files),revision=revision)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT/'outputs/offline-win10-x64')
    build(parser.parse_args().output.resolve())
