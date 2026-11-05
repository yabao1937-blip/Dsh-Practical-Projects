"""The three-asset installer keeps collection/data untouched and uses static probes."""
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest


SCRIPT = Path(__file__).resolve().parents[1]/'scripts/windows_offline/apply_update.py'
spec = importlib.util.spec_from_file_location('frontend_asset_update',SCRIPT)
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


@pytest.fixture
def prepared(tmp_path,monkeypatch):
    package,root = tmp_path/'package',tmp_path/'host'
    root.mkdir()
    (root/'offline-install.json').write_text('{}')
    (root/'Start.ps1').write_text('owned starter sentinel')
    (package/'StopOwnedWeb.ps1').parent.mkdir(parents=True)
    (package/'StopOwnedWeb.ps1').write_text('owned web stopper sentinel')
    old,new,files = {},{},{}
    for name in sorted(update.FRONTEND_ASSET_FILES):
        old[name],new[name] = ('old '+name).encode(),('new '+name).encode()
        source,target = package/'payload'/name,root/name
        source.parent.mkdir(parents=True,exist_ok=True)
        target.parent.mkdir(parents=True,exist_ok=True)
        source.write_bytes(new[name])
        target.write_bytes(old[name])
        files[name] = dict(sha256=update.digest(source),previous_sha256=update.digest(target))
    untouched = {
        'backend/data/dense_medium.db': b'not a database: it must never be opened',
        'backend/config/kepserver.local.json': b'{"poll_interval_seconds":5,"history_interval_seconds":60,"level_history_interval_seconds":30}',
        'backend/models/expert8.json': b'frozen EX8 model sentinel',
    }
    for name,data in untouched.items():
        target = root/name
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(data)
    manifest = dict(format=1,profile='query-only-hotfix',revision='frontend-test',
        frontend_asset_verification=update.FRONTEND_ASSET_VERIFICATION,files=files,
        report_url='http://127.0.0.1:9/unreachable-status')
    calls,requests = [],[]
    monkeypatch.setattr(update,'powershell',lambda *args,**kwargs:calls.append(args))
    monkeypatch.setattr(update.time,'sleep',lambda _:None)

    def forbidden(*args,**kwargs):
        raise AssertionError('A frontend update must not access database or collector control')

    for name in ('backup_database','collector_configuration','collector_status',
                 'collector_ready','wait_collector_lease','point_history_spool'):
        monkeypatch.setattr(update,name,forbidden)

    def serve(request,**kwargs):
        url = request.full_url if hasattr(request,'full_url') else request
        requests.append(url)
        path = urlsplit(url).path
        if path == '/api/v1/health/live':
            return io.BytesIO(b'{"status":"ok"}')
        name = 'frontend'+path
        if name in update.FRONTEND_ASSET_FILES:
            return io.BytesIO((root/name).read_bytes())
        raise AssertionError('Unexpected production probe: '+url)

    monkeypatch.setattr(update,'urlopen',serve)
    return SimpleNamespace(package=package,root=root,old=old,new=new,manifest=manifest,
        untouched=untouched,calls=calls,requests=requests,serve=serve)


def assert_untouched(p):
    assert all((p.root/name).read_bytes() == data for name,data in p.untouched.items())
    assert not any('Collector' in str(arg) for call in p.calls for arg in call)


def test_install_and_repeat_use_only_live_and_exact_static_assets(prepared):
    p = prepared
    first = update.apply(p.package,p.root,p.manifest)
    assert {x.relative_to(first/'source').as_posix() for x in (first/'source').rglob('*') if x.is_file()} == update.FRONTEND_ASSET_FILES
    assert all((first/'source'/name).read_bytes() == p.old[name] for name in p.old)
    assert all((p.root/name).read_bytes() == p.new[name] for name in p.new)
    assert not (first/'dense_medium.db').exists()
    second = update.apply(p.package,p.root,p.manifest)
    assert all((second/'source'/name).read_bytes() == p.new[name] for name in p.new)
    installed = json.loads((p.root/'source-update.json').read_text())
    assert installed['database_snapshot'] is False
    assert installed['collector_restarted'] is False
    assert installed['frontend_asset_verification'] == update.FRONTEND_ASSET_VERIFICATION
    assert {urlsplit(url).path for url in p.requests} == {
        '/api/v1/health/live','/index.html','/js/decision-density-trend.js','/css/decision-density-trend.css'}
    assert_untouched(p)


@pytest.mark.parametrize('fault',['changed_host','missing_host','damaged_payload','missing_payload','extra_payload'])
def test_invalid_baseline_or_payload_fails_before_any_service_or_backup(prepared,fault):
    p = prepared
    name = 'frontend/index.html'
    if fault == 'changed_host': (p.root/name).write_bytes(b'unreviewed host change')
    elif fault == 'missing_host': (p.root/name).unlink()
    elif fault == 'damaged_payload': (p.package/'payload'/name).write_bytes(b'tampered')
    elif fault == 'missing_payload': (p.package/'payload'/name).unlink()
    else: (p.package/'payload/frontend/unlisted.js').write_bytes(b'unlisted')
    with pytest.raises(ValueError): update.apply(p.package,p.root,p.manifest)
    assert not (p.root/'backups').exists()
    assert not p.calls and not p.requests
    assert_untouched(p)


@pytest.mark.parametrize('fault',[
    'wrong_flag','false_flag','wrong_profile','subset','extra_manifest_file','alias_hash',
    'missing_base','invalid_target_hash','history_setting','snapshot_policy','spool_policy',
    'function_patch','schema_rollback',
])
def test_flag_cannot_bypass_checks_for_other_updates(prepared,fault):
    p = prepared
    m = p.manifest
    info = m['files']['frontend/index.html']
    if fault == 'wrong_flag': m['frontend_asset_verification'] = 'any-assets'
    elif fault == 'false_flag': m['frontend_asset_verification'] = False
    elif fault == 'wrong_profile': m['profile'] = None
    elif fault == 'subset': m['files'].pop('frontend/css/decision-density-trend.css')
    elif fault == 'extra_manifest_file': m['files']['backend/app/main.py'] = dict(info)
    elif fault == 'alias_hash': info['accepted_previous_sha256'] = [info['previous_sha256']]
    elif fault == 'missing_base': info.pop('previous_sha256')
    elif fault == 'invalid_target_hash': info['sha256'] = 'not-a-sha'
    else:
        key,value = {
            'history_setting':('collector_history_interval_seconds',60),
            'snapshot_policy':('snapshot_content_check_policy','maintenance-window'),
            'spool_policy':('collector_spool_compatibility','point-history-v1'),
            'function_patch':('function_patch',update.PERIODIC_QUERY_PATCH),
            'schema_rollback':('rollback_compatibility',update.MANUAL_FLOAT_ROLLBACK_COMPATIBILITY),
        }[fault]
        m[key] = value
    with pytest.raises(ValueError): update.apply(p.package,p.root,m)
    assert not (p.root/'backups').exists()
    assert not p.calls and not p.requests
    assert_untouched(p)


def test_static_verification_failure_restores_three_old_sources_and_live_restart(prepared,monkeypatch):
    p = prepared

    def stale(request,**kwargs):
        url = request.full_url if hasattr(request,'full_url') else request
        if urlsplit(url).path == '/index.html':
            p.requests.append(url)
            return io.BytesIO(b'stale served file')
        return p.serve(request,**kwargs)

    monkeypatch.setattr(update,'urlopen',stale)
    with pytest.raises(RuntimeError,match='frontend asset was not loaded'):
        update.apply(p.package,p.root,p.manifest)
    assert all((p.root/name).read_bytes() == p.old[name] for name in p.old)
    assert not (p.root/'source-update.json').exists()
    assert sum(url.endswith('/health/live') for url in p.requests) == 2
    assert_untouched(p)


def test_start_failure_restores_sources_and_recovers_with_live_probe(prepared,monkeypatch):
    p = prepared
    attempts = 0

    def unavailable_then_recovered(request,**kwargs):
        nonlocal attempts
        url = request.full_url if hasattr(request,'full_url') else request
        if url.endswith('/health/live'):
            attempts += 1
            if attempts <= 30:
                p.requests.append(url)
                raise OSError('synthetic service startup failure')
        return p.serve(request,**kwargs)

    monkeypatch.setattr(update,'urlopen',unavailable_then_recovered)
    with pytest.raises(RuntimeError,match='did not become healthy'):
        update.apply(p.package,p.root,p.manifest)
    assert all((p.root/name).read_bytes() == p.old[name] for name in p.old)
    assert attempts == 31
    assert not (p.root/'source-update.json').exists()
    assert_untouched(p)


def test_preflight_main_has_no_network_service_backup_or_install_receipt(prepared,monkeypatch):
    p = prepared
    (p.package/'update.json').write_text(json.dumps(p.manifest))
    monkeypatch.setattr(update,'__file__',str(p.package/'apply_update.py'))
    monkeypatch.setattr(sys,'argv',['apply_update.py','--install-dir',str(p.root),'--preflight'])
    assert update.main() == 0
    assert not p.calls and not p.requests
    assert not (p.root/'backups').exists()
    assert not (p.root/'source-update.json').exists()
    assert_untouched(p)


def test_absent_flag_retains_existing_general_query_hotfix_checks(prepared,monkeypatch):
    p = prepared
    p.manifest.pop('frontend_asset_verification')

    def general(request,**kwargs):
        url = request.full_url if hasattr(request,'full_url') else request
        p.requests.append(url)
        path = urlsplit(url).path
        if path == '/api/v1/health': return io.BytesIO(b'{"status":"ok"}')
        if path == '/api/v1/decision/hourly': return io.BytesIO(b'{"advice_policy":"gpt-hourly-reference-v4"}')
        if path == '/': return io.BytesIO('实时总精煤灰分'.encode())
        raise AssertionError('Unexpected request')

    monkeypatch.setattr(update,'urlopen',general)
    update.apply(p.package,p.root,p.manifest)
    assert [urlsplit(url).path for url in p.requests] == [
        '/api/v1/health','/api/v1/decision/hourly','/']
    assert_untouched(p)


def test_component_success_does_not_report_whole_bundle_installed(prepared,monkeypatch,capsys):
    p = prepared
    (p.package/'update.json').write_text(json.dumps(p.manifest))
    monkeypatch.setattr(update,'__file__',str(p.package/'apply_update.py'))
    monkeypatch.setattr(sys,'argv',['apply_update.py','--install-dir',str(p.root),'--component'])
    monkeypatch.setattr(update,'apply',lambda *args,**kwargs:p.root/'backups/source-only')
    reports = []

    def callback(request,**kwargs):
        reports.append(json.loads(request.data))
        return io.BytesIO(b'')

    monkeypatch.setattr(update,'urlopen',callback)
    assert update.main() == 0
    assert reports[-1]['stage'] == 'copying'
    assert reports[-1]['healthy'] is False
    assert reports[-1]['message'].startswith('[frontend component]')
    assert all(r['stage'] != 'installed' for r in reports)
    assert 'frontend-installed:' in capsys.readouterr().out
