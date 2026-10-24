"""Build the hash-pinned, single-function minute-readiness query hotfix.

This uses committed source only and never extends the generic query allowlist.
The accepted release ZIP supplies the exact deployed baseline, including its
line endings. No database, schema, model, collector or configuration is shipped.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

from windows_offline.apply_update import (PERIODIC_QUERY_BASELINE, PERIODIC_QUERY_PATCH,
    PERIODIC_QUERY_PATH, PERIODIC_QUERY_PROOF, periodic_query_body_only)

ROOT = Path(__file__).resolve().parents[2]
INSTALLED_PACKAGE_SHA256 = '01319906b51c556f1cfa7e9ff228d5b6825d8189abfb9eb5019493fdd19f6383'


def git_bytes(revision,name):
    return subprocess.check_output(['git','show',revision+':'+name],cwd=ROOT)


def build(base,output,installed_package):
    base = subprocess.check_output(['git','rev-parse','--verify',base+'^{commit}'],cwd=ROOT,text=True).strip()
    revision = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    package_bytes = installed_package.read_bytes()
    if hashlib.sha256(package_bytes).hexdigest() != INSTALLED_PACKAGE_SHA256:
        raise ValueError('Installed minute release ZIP is not the exact accepted production package')
    with zipfile.ZipFile(installed_package) as installed:
        if len(installed.namelist()) != len(set(installed.namelist())):
            raise ValueError('Installed release ZIP contains duplicate entries')
        deployed = json.loads(installed.read('update.json'))
        if deployed.get('revision') != base:
            raise ValueError('Installed minute release revision does not match the requested baseline')
        original = installed.read('payload/'+PERIODIC_QUERY_PATH)
        if (hashlib.sha256(original).hexdigest() != PERIODIC_QUERY_BASELINE or
                deployed['files'][PERIODIC_QUERY_PATH]['sha256'] != PERIODIC_QUERY_BASELINE):
            raise ValueError('Installed service bytes do not match the exact accepted baseline')
    updated = git_bytes(revision,PERIODIC_QUERY_PATH)
    periodic_query_body_only(original,updated)
    helper = 'backend/scripts/windows_offline/apply_update.py'
    committed_updater = git_bytes(revision,helper)
    working_updater = (ROOT/helper).read_bytes()
    # Do not ship an old committed verifier while building with a newer local
    # guard. CRLF checkout conversion alone is harmless.
    if committed_updater.replace(b'\r\n',b'\n') != working_updater.replace(b'\r\n',b'\n'):
        raise ValueError('Commit the reviewed updater guard before building this hotfix')
    settings = json.loads((output/'transfer-settings.json').read_text(encoding='utf-8'))
    manifest = dict(format=1,profile='query-only-hotfix',function_patch=PERIODIC_QUERY_PATCH,
        revision=revision,baseline=base,created_at=datetime.now().astimezone().isoformat(),
        installed_package_sha256=INSTALLED_PACKAGE_SHA256,
        files={PERIODIC_QUERY_PATH:dict(sha256=hashlib.sha256(updated).hexdigest(),
                                      previous_sha256=PERIODIC_QUERY_BASELINE)},
        report_url=f'http://{settings["bind"]}:{settings["port"]}/{settings["token"]}/status')
    target = output/'extras/ProjectUpdate.zip'
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary = target.with_suffix('.tmp')
    try:
        with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('update.json',json.dumps(manifest,indent=2).encode())
            archive.writestr('payload/'+PERIODIC_QUERY_PATH,updated)
            archive.writestr(PERIODIC_QUERY_PROOF,original)
            for name in ('apply_update.py','UpdateProject.cmd','StopOwnedWeb.ps1'):
                data = git_bytes(revision,'backend/scripts/windows_offline/'+name)
                if name.endswith('.cmd'):
                    data = data.replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')
                archive.writestr(name,data)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps(dict(package=str(target),revision=revision,files=1,bytes=target.stat().st_size)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--installed-package',type=Path,required=True)
    arguments = parser.parse_args()
    build(arguments.base,arguments.output.resolve(),arguments.installed_package.resolve())
