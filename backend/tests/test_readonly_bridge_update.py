"""The bridge-only installer cannot snapshot/replace production storage."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import zipfile

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts/windows_offline'
spec = importlib.util.spec_from_file_location('readonly_update', SCRIPTS / 'enable_readonly_bridge.py')
update = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(SCRIPTS))
try:
    spec.loader.exec_module(update)
finally:
    sys.path.pop(0)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    package, root = tmp_path / 'package', tmp_path / 'host'
    root.mkdir()
    (root / 'offline-install.json').write_text('{}')
    (root / 'Start.ps1').write_text('start this installation')
    (root / 'backend/data').mkdir(parents=True)
    database = root / 'backend/data/dense_medium.db'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE evidence (value INTEGER)')
        db.execute('INSERT INTO evidence VALUES (1)')
    files = {}
    for name in sorted(update.RUNTIME_FILES):
        payload = package / 'payload' / name
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_text('new:' + name)
        target = root / name
        if '/remote_readonly/' not in name and name != 'backend/remote_mode.py':
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('old:' + name)
        files[name] = {'sha256': update.digest(payload),
                       'previous_sha256': update.digest(target) if target.exists() else None}
    manifest = {'profile': update.PROFILE, 'revision': 'fixture-revision', 'files': files}
    access_file = tmp_path / 'access.json'
    access_file.write_text(json.dumps({'token': 'fixture-secret-do-not-print-' * 2,
                                      'clients': ['127.0.0.1', '10.255.2.6']}))
    calls = []
    monkeypatch.setattr(update, 'powershell', lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(update, 'restart', lambda root: calls.append(('restart', root)))
    monkeypatch.setattr(update, 'check_bridge', lambda root, access: None)
    return package, root, manifest, access_file, calls


@pytest.mark.parametrize('name', ['backend/data/dense_medium.db', 'backend/models/model.json',
    'backend/config/kepserver.local.json', '../backend/serve.py', 'backend/../serve.py',
    'C:/Windows/test.py', 'frontend/js/api.js', 'backend/migrations/new.py'])
def test_profile_rejects_other_sources(tmp_path, name):
    with pytest.raises(ValueError):
        update.source_path(tmp_path, name)


def test_installer_never_opens_database_and_only_stops_owned_web(prepared, monkeypatch):
    package, root, manifest, access_file, calls = prepared
    before = (root / 'backend/data/dense_medium.db').read_bytes()
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: pytest.fail('DB must not be opened'))
    backup = update.apply(package, root, manifest, access_file)
    assert (root / 'backend/data/dense_medium.db').read_bytes() == before
    assert not (backup / 'dense_medium.db').exists()
    assert (backup / 'files.json').is_file()
    assert (root / update.ACCESS_PATH).read_text() == access_file.read_text() or json.loads(
        (root / update.ACCESS_PATH).read_text()) == json.loads(access_file.read_text())
    assert calls[0][0] == package / 'StopOwnedWeb.ps1'
    assert all('collector' not in str(call).lower() for call in calls)
    for name in update.RUNTIME_FILES:
        assert (root / name).read_text() == 'new:' + name


@pytest.mark.parametrize('failure', ['modified', 'damaged', 'new-conflict', 'config-conflict', 'profile'])
def test_preflight_failure_changes_nothing(prepared, failure):
    package, root, manifest, access_file, calls = prepared
    if failure == 'modified':
        (root / 'backend/serve.py').write_text('custom host edit')
    elif failure == 'damaged':
        (package / 'payload/backend/serve.py').write_text('damage')
    elif failure == 'new-conflict':
        (root / 'backend/remote_mode.py').write_text('pre-existing file')
    elif failure == 'config-conflict':
        target = root / update.ACCESS_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('do not replace')
    else:
        manifest['files']['backend/models/model.json'] = {}
    with pytest.raises(ValueError):
        update.apply(package, root, manifest, access_file)
    assert not calls
    assert not (root / 'backups').exists()


def test_restart_failure_restores_source_and_config_but_retains_fresh_rows(prepared, monkeypatch):
    package, root, manifest, access_file, calls = prepared
    count = 0
    def restart(root):
        nonlocal count
        count += 1
        if count == 1:
            with sqlite3.connect(root / 'backend/data/dense_medium.db') as db:
                db.execute('INSERT INTO evidence VALUES (2)')
            raise RuntimeError('simulated failure')
    monkeypatch.setattr(update, 'restart', restart)
    with pytest.raises(RuntimeError):
        update.apply(package, root, manifest, access_file)
    assert count == 2 and not (root / update.ACCESS_PATH).exists()
    for name, info in manifest['files'].items():
        target = root / name
        if info['previous_sha256'] is None:
            assert not target.exists()
        else:
            assert target.read_text() == 'old:' + name
    with sqlite3.connect(root / 'backend/data/dense_medium.db') as db:
        assert db.execute('SELECT * FROM evidence').fetchall() == [(1,), (2,)]


@pytest.mark.parametrize('data', ['not json', '{"token":"sensitive"}',
    '{"token":"too-short","clients":["10.255.2.6"]}',
    '{"token":"abcdefghijklmnopqrstuvwxyz0123456789","clients":["10.255.2.0/24"]}'])
def test_invalid_access_never_echoes_input(tmp_path, data):
    path = tmp_path / 'access.json'
    path.write_text(data)
    with pytest.raises(ValueError, match='^Invalid dedicated read-only access configuration$'):
        update.read_access(path)


def load_builder():
    spec = importlib.util.spec_from_file_location('readonly_builder', SCRIPTS.parent / 'package_readonly_bridge.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def baseline_output(tmp_path):
    output, stage = tmp_path / 'output', tmp_path / 'stage'
    (output / 'extras').mkdir(parents=True)
    stage.mkdir()
    (output / 'latest.json').write_text(json.dumps({'stage': str(stage)}))
    (stage / 'manifest.json').write_text(json.dumps({'files': {
        'project/backend/app/config.py': 'original-config',
        'project/backend/app/main.py': 'original-main',
        'project/backend/serve.py': 'original-serve',
    }}))
    for filename, files in [('ProjectUpdate.zip', {'backend/app/main.py': {'sha256': 'current-main'}}),
            ('ProjectUpdate-c9a7bb9.zip', {'backend/app/main.py': {'sha256': 'parent-main'},
                                       'backend/app/config.py': {'sha256': 'parent-config'}})]:
        with zipfile.ZipFile(output / 'extras' / filename, 'w') as archive:
            archive.writestr('update.json', json.dumps({'files': files}))
    return output


def test_baseline_precedence_is_current_then_parent_then_original(tmp_path):
    builder = load_builder()
    previous = builder.previous_hashes(baseline_output(tmp_path))
    assert previous['backend/app/main.py'] == 'current-main'
    assert previous['backend/app/config.py'] == 'parent-config'
    assert previous['backend/serve.py'] == 'original-serve'
    assert previous.get('backend/remote_mode.py') is None


def test_package_uses_only_verified_commit_and_never_prints_access_token(tmp_path, monkeypatch, capsys):
    builder = load_builder()
    output = baseline_output(tmp_path)
    access = tmp_path / 'access.json'
    token = 'super-private-test-token-' * 2
    access.write_text(json.dumps({'token': token, 'clients': ['127.0.0.1', '10.255.2.6']}))
    requested = []
    def git_bytes(revision, name, *, root):
        assert revision == 'f' * 40
        requested.append(name)
        return ('committed:' + name).encode()
    def check_output(args, **kwargs):
        assert args == ['git', 'rev-parse', '--verify', 'chosen^{commit}']
        return 'f' * 40 + '\n'
    monkeypatch.setattr(builder, 'git_bytes', git_bytes)
    monkeypatch.setattr(builder.subprocess, 'check_output', check_output)
    package = builder.build(output, 'chosen', access, root=tmp_path)
    assert token not in capsys.readouterr().out
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read('readonly-update.json'))
        assert manifest['profile'] == update.PROFILE
        assert manifest['revision'] == 'f' * 40
        assert set(manifest['files']) == update.RUNTIME_FILES
        assert json.loads(archive.read('access-config.json'))['token'] == token
        for name in update.RUNTIME_FILES:
            assert archive.read('payload/' + name) == ('committed:' + name).encode()
        assert not any(name.endswith(('.db', '.db-wal', '.db-shm')) for name in archive.namelist())
        assert not any('kepserver.local' in name or '/migrations/' in name for name in archive.namelist())
    assert set(requested) == update.RUNTIME_FILES | {
        'backend/scripts/windows_offline/enable_readonly_bridge.py',
        'backend/scripts/windows_offline/EnableReadOnlyTest.cmd',
        'backend/scripts/windows_offline/apply_update.py',
        'backend/scripts/windows_offline/StopOwnedWeb.ps1',
    }


def test_builder_rejects_missing_loopback_before_reading_git_or_writing_package(tmp_path):
    builder = load_builder()
    output = baseline_output(tmp_path)
    access = tmp_path / 'access.json'
    access.write_text(json.dumps({'token': 'private-fixture-' * 3, 'clients': ['10.255.2.6']}))
    with pytest.raises(ValueError, match='local verification probe'):
        builder.build(output, 'unused', access, root=tmp_path)
    assert not (output / 'extras/ReadOnlyBridge.zip').exists()


def test_restart_calls_only_web_entry_point(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(update, 'powershell', lambda *a, **k: calls.append(a))
    import io
    monkeypatch.setattr(update, 'urlopen', lambda *a, **k: io.BytesIO(b'{"status":"ok"}'))
    update.restart(tmp_path)
    assert calls == [(tmp_path / 'backend/scripts/start_server.ps1', '-NoBrowser')]
