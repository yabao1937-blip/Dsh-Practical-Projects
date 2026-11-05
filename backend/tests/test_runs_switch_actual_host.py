"""The four measured host sources, exact ZIP selection and isolated recovery."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from app.models import Setting
from app.telemetry.config import CollectorConfig
from app.telemetry.domain import iso_time
from app.telemetry.repository import TelemetryRepository
from test_desliming_runs_switch import ROOT, switch, host, sessions, seeded, HOUR, retired_points

CAPTURE = ROOT/'output/runtime-source-capture-20261103/received/capture-0a5d51cbb52075ee'
CANDIDATE = Path(os.environ.get('DMCS_RUNS_COMPAT_CANDIDATE',
    str(ROOT/'output/runs-switch-20261103-candidate-v4-pretest/SwitchDeslimingRuns.zip')))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def actual_host(host, monkeypatch):
    with zipfile.ZipFile(CANDIDATE) as archive:
        archive.extractall(host.package)
    host.manifest = json.loads((host.package/'bundle.json').read_text())
    for name in switch.SOURCE_FILES:
        (host.root/name).write_bytes((CAPTURE/'sources'/name).read_bytes())
    module = load_module('packaged_actual_host_switch',host.package/'switch_runs.py')
    monkeypatch.setattr(module,'start_collector',switch.start_collector)
    monkeypatch.setattr(module,'start_web',switch.start_web)
    host.module = module
    return host


def test_original_v3_rejects_the_measured_host_without_mutation(host):
    with zipfile.ZipFile(ROOT/'output/runs-switch-20261103-candidate-v3/SwitchDeslimingRuns.zip') as archive:
        archive.extractall(host.package)
    manifest = json.loads((host.package/'bundle.json').read_text())
    old = load_module('original_v3_actual_rejection',host.package/'switch_runs.py')
    originals = {}
    for name in switch.SOURCE_FILES:
        originals[name] = (CAPTURE/'sources'/name).read_bytes()
        (host.root/name).write_bytes(originals[name])
    with pytest.raises(old.SwitchError,match='Unknown installed source version'):
        old.preflight(host.root,host.package,manifest)
    assert host.events == [] and host.config.read_bytes() == host.original
    assert originals == {name:(host.root/name).read_bytes() for name in originals}


def test_actual_package_preflight_success_and_repeat_keep_the_captured_pair(actual_host):
    host = actual_host
    module = host.module
    task = module.preflight(host.root,host.package,host.manifest)
    assert {source['variant'] for source in task['sources'].values()} >= module.CAPTURED_VARIANTS
    state = module.database_state(host.root)
    assert module.apply(host.root,host.package,host.manifest,host.control,dry_run=True)['status'] == 'preflight-passed'
    assert host.events == [] and module.database_state(host.root) == state
    result = module.apply(host.root,host.package,host.manifest,host.control)
    assert result['status'] == 'installed'
    installed = {name:(host.root/name).read_bytes() for name in module.SOURCE_FILES}
    assert installed == {name:source['updated'] for name,source in task['sources'].items()}
    marker = module.database_state(host.root)['marker']
    host.events.clear()
    assert module.apply(host.root,host.package,host.manifest,host.control)['status'] == 'unchanged-verified'
    assert marker == module.database_state(host.root)['marker']
    assert installed == {name:(host.root/name).read_bytes() for name in installed}
    assert host.events == []


def test_actual_package_post_web_collector_loss_restores_all_four_original_files(actual_host, monkeypatch):
    host = actual_host
    module = host.module
    original_state = module.database_state(host.root)
    original_sources = {name:(host.root/name).read_bytes() for name in module.SOURCE_FILES}
    first = True
    reports = []
    def web_starts(root, control):
        nonlocal first
        host.events.append('start-web')
        if first:
            first = False
            owner = control.collector_status(root,module.SOURCE_ID)['owner']
            cfg = CollectorConfig.model_validate_json(host.config.read_bytes())
            TelemetryRepository(host.sessions).state(cfg,owner,'stopped',release=True)
    monkeypatch.setattr(module,'start_web',web_starts)
    with pytest.raises(module.SwitchError,match='ownership'):
        module.apply(host.root,host.package,host.manifest,host.control,lambda stage,message:reports.append(stage))
    assert original_sources == {name:(host.root/name).read_bytes() for name in original_sources}
    assert module.database_state(host.root) == original_state
    assert host.config.read_bytes() == host.original
    assert host.events[-3:] == ['start-collector','ready','start-web']
    assert 'installed' not in reports


@pytest.mark.parametrize('mixed', ['primary-inputs','primary-derived','new-inputs-only','new-derived-only'])
def test_captured_and_press_alias_source_families_cannot_be_mixed(actual_host,mixed):
    host = actual_host
    derived,inputs = 'backend/app/telemetry/derived.py','backend/app/hourly_brief/inputs.py'
    if mixed.startswith('primary'):
        name = inputs if mixed == 'primary-inputs' else derived
        (host.root/name).write_bytes(subprocess.check_output(['git','show','HEAD:'+name],cwd=ROOT))
        reason = 'selected together'
    else:
        name = inputs if mixed == 'new-inputs-only' else derived
        variant = 'variants/inputs-host-1c03e358.py' if name == inputs else 'variants/derived-host-1a00abe3.py'
        (host.root/name).write_bytes((host.package/variant).read_bytes())
        reason = 'partially updated'
    before = {name:(host.root/name).read_bytes() for name in host.module.SOURCE_FILES}
    with pytest.raises(host.module.SwitchError,match=reason):
        host.module.apply(host.root,host.package,host.manifest,host.control,dry_run=True)
    assert host.events == []
    assert before == {name:(host.root/name).read_bytes() for name in before}


def test_actual_source_still_rejects_a_single_unreviewed_byte_change(actual_host):
    host = actual_host
    name = 'backend/app/hourly_brief/inputs.py'
    changed = (host.root/name).read_bytes()+b'# unreviewed external edit\n'
    (host.root/name).write_bytes(changed)
    with pytest.raises(host.module.SwitchError,match='Unknown installed source') as error:
        host.module.apply(host.root,host.package,host.manifest,host.control,dry_run=True)
    facts = json.loads(str(error.value).split('; actual=',1)[1])
    assert facts['sha256'] == hashlib.sha256(changed).hexdigest()
    assert facts['bytes'] == len(changed) and facts['path'] == name
    assert (host.root/name).read_bytes() == changed and host.events == []


def test_actual_variants_preserve_every_byte_outside_the_reviewed_edits(actual_host):
    proof = json.loads((CANDIDATE.parent/'host-source-compatibility-proof.json').read_text())
    assert hashlib.sha256((CAPTURE/'capture-report.json').read_bytes()).hexdigest() == proof['capture_report_sha256']
    for variant,record in proof['variants'].items():
        original = (CAPTURE/'sources'/record['source']).read_bytes()
        updated = (actual_host.package/variant).read_bytes()
        old_position,new_position = 0,0
        for edit in record['edits']:
            unchanged = original[old_position:edit['start']]
            assert updated[new_position:new_position+len(unchanged)] == unchanged
            new_position += len(unchanged)
            replacement = updated[new_position:new_position+edit['added_bytes']]
            assert hashlib.sha256(replacement).hexdigest() == edit['added_sha256']
            assert hashlib.sha256(original[edit['start']:edit['end']]).hexdigest() == edit['removed_sha256']
            old_position,new_position = edit['end'],new_position+edit['added_bytes']
        assert original[old_position:] == updated[new_position:]
        assert hashlib.sha256(original).hexdigest() == record['before_sha256']
        assert hashlib.sha256(updated).hexdigest() == record['after_sha256']
    derived = (actual_host.package/'variants/derived-host-1a00abe3.py').read_bytes()
    inputs = (actual_host.package/'variants/inputs-host-1c03e358.py').read_bytes()
    assert b'PRESS_STATE_ITEM_ALIASES' not in derived and b'PRESS_STATE_ITEM_ALIASES' not in inputs
    assert derived.count(b'\n') == derived.count(b'\r\n')
    assert b'\r\n' not in inputs


def test_captured_runtime_pair_imports_and_uses_runs_with_original_historical_identity(actual_host,sessions,seeded,monkeypatch):
    import app.telemetry
    import app.hourly_brief
    derived = load_module('app.telemetry.actual_host_derived',actual_host.package/'variants/derived-host-1a00abe3.py')
    monkeypatch.setitem(sys.modules,'app.telemetry.derived',derived)
    monkeypatch.setattr(app.telemetry,'derived',derived)
    inputs = load_module('app.hourly_brief.actual_host_inputs',actual_host.package/'variants/inputs-host-1c03e358.py')
    monkeypatch.setitem(sys.modules,'app.hourly_brief.inputs',inputs)
    monkeypatch.setattr(app.hourly_brief,'inputs',inputs)
    sources = load_module('app.services.actual_host_sources',actual_host.package/'payload/backend/app/services/decision_sources.py')
    with sessions.begin() as db:
        retired_points(db,HOUR-timedelta(seconds=2))
        db.add(Setting(key=derived.DESLIMING_CUTOVER_SETTING,value=dict(version=derived.DESLIMING_RUNS_VERSION,
            source_id='kepserver-main',effective_at=iso_time(HOUR-timedelta(seconds=1)))))
    with sessions() as db:
        old = inputs.Inputs(db,HOUR-timedelta(seconds=2),authoritative=True).group(derived.DESLIMING_MEMBERS)
        current = sources.shared_snapshot(db,HOUR,live=True)
        assert old['value'] == {'473':False,'474':True}
        assert {c['item_id'] for c in old['components'].values()} == {p[1] for p in derived.LEGACY_DESLIMING_MEMBERS}
        assert current['cells']['desliming']['value'] == {'473':True,'474':False}
        assert current['inputs']['desliming473']['item_id'] == derived.DESLIMING_MEMBERS[0][1]
        assert current['inputs']['desliming473']['value'] == 1 and current['inputs']['desliming474']['value'] == 0
