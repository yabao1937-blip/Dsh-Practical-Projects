"""One serial entry point reports partial results without inventing a global rollback."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1]/'scripts/host_trend_runs_update/update_trend_runs.py'
spec = importlib.util.spec_from_file_location('trend_runs_coordinator',SCRIPT)
coordinator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coordinator)


@pytest.fixture
def prepared(tmp_path,monkeypatch):
    package,root = tmp_path/'package',tmp_path/'host'
    root.mkdir()
    (root/'offline-install.json').write_text('{}')
    (root/'Start.ps1').write_text('existing owned starter')
    interpreter = root/'backend/.venv/Scripts/python.exe'
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b'installed interpreter sentinel')
    (root/'backend/data').mkdir()
    database = root/'backend/data/dense_medium.db'
    database.write_bytes(b'production DB sentinel, never opened')
    for _,name,_ in coordinator.COMPONENTS:
        path = package/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'independent component sentinel')
    (package/'frontend/update.json').write_text(json.dumps(dict(report_url='http://127.0.0.1:9/private-status')))
    files = {p.relative_to(package).as_posix():coordinator.digest(p) for p in package.rglob('*') if p.is_file()}
    (package/'bundle.json').write_text(json.dumps(dict(format=1,type=coordinator.BUNDLE_TYPE,revision='combined-test',files=files)))
    calls,reports = [],[]

    def run_component(command,log,label):
        calls.append((command,log,label))
        return 0

    monkeypatch.setattr(coordinator,'run_component',run_component)
    monkeypatch.setattr(coordinator,'callback',lambda pkg,status:reports.append(copy.deepcopy(status)))
    return SimpleNamespace(package=package,root=root,interpreter=interpreter,
        database=database,calls=calls,reports=reports,run_component=run_component)


def status(p):
    return json.loads((p.package/'combined-update-status.json').read_text())


def assert_no_global_transaction(p):
    assert p.database.read_bytes() == b'production DB sentinel, never opened'
    assert not (p.root/'backups').exists()
    assert not (p.root/'trend-runs-update.lock').exists()


def test_both_preflights_precede_serial_components_and_only_final_callback_installs(prepared):
    p = prepared
    assert coordinator.run(p.package,p.root) == 0
    assert [label for _,_,label in p.calls] == ['runs preflight','frontend preflight','runs','frontend']
    assert [command[-1] for command,_,_ in p.calls] == ['--dry-run','--preflight','--component','--component']
    assert all(command[0] == str(p.interpreter) for command,_,_ in p.calls)
    result = status(p)
    assert result['stage'] == 'installed'
    assert all(value['preflight']=='passed' and value['apply']=='installed' for value in result['components'].values())
    assert [report['stage'] for report in p.reports].count('installed') == 1
    assert p.reports[-1]['stage'] == 'installed'
    assert_no_global_transaction(p)


@pytest.mark.parametrize('failure_index',range(4))
def test_any_failure_stops_remaining_steps_and_reports_components(prepared,monkeypatch,failure_index):
    p = prepared

    def fail_step(command,log,label):
        p.calls.append((command,log,label))
        return 7 if len(p.calls)-1 == failure_index else 0

    monkeypatch.setattr(coordinator,'run_component',fail_step)
    assert coordinator.run(p.package,p.root) == 1
    assert len(p.calls) == failure_index+1
    result = status(p)
    assert result['stage'] == 'failed'
    assert all(report['stage'] != 'installed' for report in p.reports)
    if failure_index < 2:
        assert all(value['apply']=='not_started' for value in result['components'].values())
    elif failure_index == 2:
        assert result['components']['runs']['apply'] == 'failed'
        assert result['components']['frontend']['apply'] == 'not_started'
    else:
        assert result['components']['runs']['apply'] == 'installed'
        assert result['components']['frontend']['apply'] == 'failed'
        assert 'Completed components: runs' in result['message']
    assert_no_global_transaction(p)


@pytest.mark.parametrize('failure_index',[0,2,3])
def test_process_launch_failure_has_same_partial_state_rules(prepared,monkeypatch,failure_index):
    p = prepared

    def launch_failure(command,log,label):
        p.calls.append((command,log,label))
        if len(p.calls)-1 == failure_index: raise OSError('synthetic launch error')
        return 0

    monkeypatch.setattr(coordinator,'run_component',launch_failure)
    assert coordinator.run(p.package,p.root) == 1
    assert len(p.calls) == failure_index+1
    assert status(p)['stage'] == 'failed'
    assert all(report['stage'] != 'installed' for report in p.reports)
    assert_no_global_transaction(p)


def test_existing_lock_allows_preflight_but_never_applies_or_removes_other_owner(prepared):
    p = prepared
    lock = p.root/'trend-runs-update.lock'
    owner = b'{"pid":9999,"token":"other owner"}'
    lock.write_bytes(owner)
    assert coordinator.run(p.package,p.root) == 1
    assert [label for _,_,label in p.calls] == ['runs preflight','frontend preflight']
    assert lock.read_bytes() == owner
    assert all(value['apply']=='not_started' for value in status(p)['components'].values())
    assert not (p.root/'backups').exists()


def test_tampered_bundle_is_rejected_before_any_component_or_callback(prepared):
    p = prepared
    (p.package/'runs/switch_runs.py').write_bytes(b'tampered')
    with pytest.raises(ValueError,match='damaged combined update'):
        coordinator.run(p.package,p.root)
    assert not p.calls and not p.reports
    assert not (p.package/'combined-update-status.json').exists()
    assert_no_global_transaction(p)
