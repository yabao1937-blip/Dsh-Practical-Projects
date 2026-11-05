"""Build the reviewed host-local RUNS switch component without publishing it."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]
CONTROL_REVISION = '1ca7174'
BASELINES = {
    # Press-channel restoration is unrelated to the RUNS switch. Refuse a
    # still-renamed/aliased press baseline instead of silently changing it.
    'backend/app/telemetry/derived.py': '1964bbe',
    'backend/app/hourly_brief/inputs.py': '6dd6411',
    'backend/app/services/decision_sources.py': '6dd6411',
    'backend/app/decision_hourly/history.py': '6dd6411',
}
WEB_HELPER_SHA256 = '28173f157366e76f5b5a02f95def5c8a29c9b4eb1763821a7ee522481fd2ae52'
CAPTURE = ROOT/'output/runtime-source-capture-20261103/received/capture-0a5d51cbb52075ee'
CAPTURE_SHA256 = '0a5d51cbb52075ee5e2b1ddf683bbe7825ec60cda79e9571eafcf95e56a68a51'
HOST_SOURCES = {
    'backend/app/telemetry/derived.py': '1a00abe3e7b0278baffa2cd3052f401381fc4be8d3073fe9278d25e6d6dea34e',
    'backend/app/hourly_brief/inputs.py': '1c03e3589ab56c7e75367ee1895ef83f8e95df5d51acda943c6be2bc6cbaa819',
    'backend/app/services/decision_sources.py': '780c3a57e2dba3c09e5473c3ac9a40a8c138a6f50d691aa80344d5ecfdab65e4',
    'backend/app/decision_hourly/history.py': '6c498be210e4fa1dcd2a2c50b913b9915523815dfd3daaab1a6daca5e22cfe9f',
}


def byte_patch(original, replacements):
    """Apply exact reviewed edits; record offsets proving all other bytes survive."""
    edits = []
    for old,new in replacements:
        if not old or original.count(old) != 1: raise ValueError('Host patch precondition differs')
        start = original.index(old)
        edits.append((start,start+len(old),new))
    edits.sort()
    result,position,proof = [],0,[]
    for start,end,new in edits:
        if start < position: raise ValueError('Overlapping host patch ranges')
        result.extend((original[position:start],new))
        proof.append(dict(start=start,end=end,removed_bytes=end-start,added_bytes=len(new),
            removed_sha256=hashlib.sha256(original[start:end]).hexdigest(),added_sha256=hashlib.sha256(new).hexdigest()))
        position = end
    result.append(original[position:])
    return b''.join(result),proof


def host_source_variants(payload):
    if hashlib.sha256((CAPTURE/'capture-report.json').read_bytes()).hexdigest() != CAPTURE_SHA256:
        raise ValueError('Actual host capture report changed')
    actual = {}
    for name,digest in HOST_SOURCES.items():
        raw = (CAPTURE/'sources'/name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest: raise ValueError('Actual host source evidence changed: '+name)
        actual[name] = raw
    derived = 'backend/app/telemetry/derived.py'
    donor = payload['payload/'+derived]
    donor_block = donor[donor.index(b'DESLIMING_MEMBERS = ('):donor.index(b'FLOAT_PRESS_MEMBERS = ')]
    old = actual[derived]
    old_block = old[old.index(b'DESLIMING_MEMBERS = ('):old.index(b'FLOAT_PRESS_MEMBERS = ')]
    new_block = donor_block.replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')
    new_derived,derived_proof = byte_patch(old,[(old_block,new_block)])
    inputs = 'backend/app/hourly_brief/inputs.py'
    donor = payload['payload/'+inputs].replace(b'\r\n',b'\n')
    import_old = b'from ..telemetry.derived import FOUR_SYSTEM_MEMBERS, DESLIMING_MEMBERS, FLOAT_PRESS_MEMBERS, DENSITY_AVERAGE_MEMBERS\n'
    import_new = (b'from ..telemetry.derived import (FOUR_SYSTEM_MEMBERS, DESLIMING_MEMBERS, FLOAT_PRESS_MEMBERS, DENSITY_AVERAGE_MEMBERS,\n'
        b'    LEGACY_DESLIMING_MEMBERS, LEGACY_DESLIMING_KEYS, DESLIMING_CUTOVER_SETTING, DESLIMING_RUNS_VERSION)\n')
    marker_start = donor.index(b'        # The immutable installation marker')
    marker_end = donor.index(b'        self.points = {}',marker_start)
    register_start = donor.index(b'        if self.legacy_desliming:')
    register_end = donor.index(b'        self.sources = ',register_start)
    members_start = donor.index(b'    def status_members(')
    members_end = donor.index(b'        parts = ',members_start)
    cutoff = b'        self.cutoff = iso_time(hour)\n'
    simple_registration = b'            self.points.setdefault(p.config.get("item_id"), []).append(p)\n'
    old_group = b'    def group(self, members, source_id=None):\n'
    new_inputs,inputs_proof = byte_patch(actual[inputs],[
        (import_old,import_new),
        (cutoff,cutoff+donor[marker_start:marker_end]),
        (simple_registration,simple_registration+donor[register_start:register_end]),
        (old_group,donor[members_start:members_end]),
    ])
    variants = {'variants/derived-host-1a00abe3.py':(derived,new_derived,derived_proof),
        'variants/inputs-host-1c03e358.py':(inputs,new_inputs,inputs_proof)}
    proof = dict(capture_report_sha256=CAPTURE_SHA256,
        capture_directory=CAPTURE.relative_to(ROOT).as_posix(),host_source_sha256=HOST_SOURCES,variants={})
    for variant,(name,updated,edits) in variants.items():
        compile(updated,variant,'exec')
        payload[variant] = updated
        proof['variants'][variant] = dict(source=name,before_sha256=HOST_SOURCES[name],
            after_sha256=hashlib.sha256(updated).hexdigest(),before_bytes=len(actual[name]),after_bytes=len(updated),edits=edits)
    return {variant:[HOST_SOURCES[name]] for variant,(name,_,_) in variants.items()},proof


def legacy_derived_variant(current, *, crlf=False):
    """Keep the deployed 6dd press definitions byte-for-byte outside desliming."""
    name = 'backend/app/telemetry/derived.py'
    baseline = subprocess.check_output(['git','show','1964bbe:'+name],cwd=ROOT)
    legacy = subprocess.check_output(['git','show','6dd6411:'+name],cwd=ROOT)
    if crlf:
        baseline = baseline.replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')
        legacy = legacy.replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')
        current = current.replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')
    begin = baseline.index(b'DESLIMING_MEMBERS = (')
    end = baseline.index(b'FLOAT_PRESS_MEMBERS = ',begin)
    updated_begin = current.index(b'DESLIMING_MEMBERS = (')
    updated_end = current.index(b'FLOAT_PRESS_MEMBERS = ',updated_begin)
    old_section = baseline[begin:end]
    if legacy.count(old_section) != 1: raise ValueError('Legacy desliming baseline differs')
    return legacy.replace(old_section,current[updated_begin:updated_end],1), legacy


def build(output):
    directory = ROOT/'backend/scripts/host_runs_switch'
    spec = importlib.util.spec_from_file_location('runs_switch_package', directory/'switch_runs.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if set(BASELINES) != module.SOURCE_FILES: raise ValueError('Baseline and payload scope differ')
    payload = {name:(directory/name).read_bytes() for name in
        ('switch_runs.py','SwitchDeslimingRuns.cmd','StopOwnedWeb.ps1','README.txt')}
    payload['SwitchDeslimingRuns.cmd'] = payload['SwitchDeslimingRuns.cmd'].replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')
    if hashlib.sha256(payload['StopOwnedWeb.ps1']).hexdigest() != WEB_HELPER_SHA256:
        raise ValueError('Owned Web helper differs from the real-process-reviewed bytes')
    for source,target in (('apply_update.py','collector_control.py'),('StopOwnedCollector.ps1','StopOwnedCollector.ps1')):
        payload[target] = subprocess.check_output(['git','show',CONTROL_REVISION+':backend/scripts/windows_offline/'+source],cwd=ROOT)
    for name in module.SOURCE_FILES:
        payload['payload/'+name] = (ROOT/name).read_bytes()
    variant_before = {}
    for name,crlf in zip(module.VARIANT_FILES[:2],(False,True)):
        variant,legacy_before = legacy_derived_variant(payload['payload/'+module.VARIANT_SOURCE],crlf=crlf)
        payload[name] = variant
        variant_before[name] = [hashlib.sha256(legacy_before).hexdigest()]
    actual_before,host_proof = host_source_variants(payload)
    variant_before.update(actual_before)
    if set(payload) != module.BUNDLE_FILES: raise ValueError('Unexpected RUNS payload')
    for name,data in payload.items():
        if name.endswith('.py'): compile(data,name,'exec')
    before = {}
    for name,revision in BASELINES.items():
        raw = subprocess.check_output(['git','show',revision+':'+name],cwd=ROOT)
        crlf = raw.replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')
        before[name] = sorted({hashlib.sha256(raw).hexdigest(),hashlib.sha256(crlf).hexdigest()})
    settings = json.loads((ROOT/'outputs/offline-win10-x64/transfer-settings.json').read_text(encoding='utf-8'))
    manifest = dict(format=1,purpose='desliming-runs-switch-v1',version='desliming-runs-switch-20261103-v4',
        created_at=datetime.now(timezone.utc).isoformat(),
        report_url=f'http://{settings["bind"]}:{settings["port"]}/{settings["token"]}/status',
        files={name:hashlib.sha256(data).hexdigest() for name,data in payload.items()},
        source_before_sha256=before,baseline_revisions=BASELINES,
        variant_before_sha256=variant_before,
        variant_policy='Exact finite source hashes select matching old/new variants. Actual captured host derived/Inputs retain all unrelated bytes and their original absence of press aliases.',
        actual_host_source_capture=dict(report_sha256=CAPTURE_SHA256,source_sha256=HOST_SOURCES,
            provenance='User-run four-file read-only capture, uploaded to the existing diagnostics endpoint; original bytes verified locally.'),
        baseline_policy='Finite trusted source revisions; actual host source bytes must pass zero-write local preflight. Unknown bytes are refused.',
        collector_control_revision=CONTROL_REVISION,source_id=module.SOURCE_ID,
        removed_points=module.OLD_POINTS,existing_runs_points=module.RUNS_POINTS,
        model_coefficients='unchanged; no model artifact or activation payload',
        database_scope='one immutable setting, optional appended current desliming source revision, two retired point enabled flags only')
    output.mkdir(parents=True,exist_ok=True)
    target = output/'SwitchDeslimingRuns.zip'
    if target.exists(): raise FileExistsError('Use a fresh candidate output; never replace a reviewed ZIP')
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('bundle.json',json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8'))
        for name,data in payload.items(): archive.writestr(name,data)
    public = dict(manifest,report_url=f'http://{settings["bind"]}:{settings["port"]}/<redacted>/status')
    (output/'bundle.redacted.json').write_text(json.dumps(public,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (output/'host-source-compatibility-proof.json').write_text(json.dumps(host_proof,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    evidence = dict(status='candidate-not-published',package=str(target),bytes=target.stat().st_size,
        sha256=hashlib.sha256(target.read_bytes()).hexdigest(),files=sorted(payload),
        host_installer_executed=False,host_source_hashes_captured=True,capture_report_sha256=CAPTURE_SHA256)
    (output/'package-evidence.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(evidence,ensure_ascii=False,indent=2))
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'output/runs-switch-20261103-candidate-v4')
    build(parser.parse_args().output.resolve())
