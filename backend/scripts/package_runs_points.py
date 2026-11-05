"""Build a narrow host-local RUNS addition package, without publishing it."""
import argparse
from datetime import datetime,timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]
CONTROL_REVISION = '1ca7174'


def build(output):
    directory = ROOT/'backend/scripts/host_runs_points'
    spec = importlib.util.spec_from_file_location('runs_package_source',directory/'add_runs_points.py')
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    payload = {name:(directory/name).read_bytes() for name in ('add_runs_points.py','AddRunsPoints.cmd','README.txt')}
    payload['points-delta.json'] = (json.dumps(tool.POINTS,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    for source,target in (('apply_update.py','collector_control.py'),('StopOwnedCollector.ps1','StopOwnedCollector.ps1')):
        payload[target] = subprocess.check_output(['git','show',CONTROL_REVISION+':backend/scripts/windows_offline/'+source],cwd=ROOT)
    assert set(payload) == tool.BUNDLE_FILES
    for name,data in payload.items():
        if name.endswith('.py'): compile(data,name,'exec')
    settings = json.loads((ROOT/'outputs/offline-win10-x64/transfer-settings.json').read_text(encoding='utf-8'))
    manifest = dict(format=1,purpose='desliming-runs-addition-v1',version='runs-points-20261102-v1',
        created_at=datetime.now(timezone.utc).isoformat(),
        report_url=f'http://{settings["bind"]}:{settings["port"]}/{settings["token"]}/status',
        files={name:hashlib.sha256(data).hexdigest() for name,data in payload.items()},
        source_id=tool.SOURCE_ID,collector_control_revision=CONTROL_REVISION,
        type_evidence=dict(data_type='i=1',namespace=0,variant_type='Boolean',quality='Good',
            raw_value=False,probe='runs-direct-probe.json; 2026-11-02; read-only two exact nodes'),
        model_bindings='unchanged; original if_prod retained')
    output.mkdir(parents=True,exist_ok=True)
    target = output/'AddRunsPoints.zip'
    if target.exists(): raise FileExistsError('Use a new candidate directory; do not replace a reviewed ZIP')
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('bundle.json',json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8'))
        for name,data in payload.items(): archive.writestr(name,data)
    public = dict(manifest,report_url=f'http://{settings["bind"]}:{settings["port"]}/<redacted>/status')
    (output/'bundle.redacted.json').write_text(json.dumps(public,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    evidence = dict(status='candidate-not-published',package=str(target),bytes=target.stat().st_size,
        sha256=hashlib.sha256(target.read_bytes()).hexdigest(),files=sorted(payload),
        target_nodes=[p['node_id'] for p in tool.POINTS],target_keys=[p['key'] for p in tool.POINTS],
        runtime_type='Boolean verified; actual equipment meaning awaits site comparison',
        production_installer_executed=False)
    (output/'package-evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(evidence,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'output/runs-points-20261102')
    build(parser.parse_args().output.resolve())
