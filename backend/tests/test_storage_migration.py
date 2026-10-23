"""Temporary-only Windows data relocation, junction and rollback checks."""
import importlib.util
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import zipfile

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/windows_offline/move_storage.py'
spec = importlib.util.spec_from_file_location('storage_migration', SCRIPT)
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)
pytestmark = pytest.mark.skipif(os.name != 'nt', reason='NTFS junction behavior is Windows-specific')


@pytest.fixture
def install(tmp_path):
    root = tmp_path / 'C installation with spaces'
    (root / 'backend/data/telemetry-spool').mkdir(parents=True)
    (root / 'backups').mkdir()
    (root / 'offline-install.json').write_text('{}')
    (root / 'Start.ps1').write_text('# fake installation; no service is started')
    (root / 'backend/data/telemetry-spool/pending.json').write_text('{"sample": 12}')
    (root / 'backend/data/write_token.txt').write_text('synthetic-token')
    (root / 'backups/old.db').write_bytes(b'synthetic-backup')
    with closing(sqlite3.connect(root / 'backend/data/dense_medium.db')) as connection:
        connection.execute('CREATE TABLE readings (value INTEGER)')
        connection.execute('INSERT INTO readings VALUES (1)')
        connection.commit()
    return root, tmp_path / 'D data with spaces'


class FakeRuntime:
    def __init__(self, root, *, fail_verify=False):
        self.root, self.fail_verify = root, fail_verify
        self.events = []

    def stop(self, *, watchdog=True):
        self.events.append(('stop', watchdog))

    def start(self):
        self.events.append(('start',))

    def verify(self, *, restarted=True):
        self.events.append(('verify', restarted))
        if self.fail_verify:
            with closing(sqlite3.connect(self.root / 'backend/data/dense_medium.db')) as connection:
                connection.execute('INSERT INTO readings VALUES (2)')
                connection.commit()
            raise RuntimeError('simulated late health failure after a new committed reading')

    def restore_watchdog(self):
        self.events.append(('watchdog',))


def run(root, destination, runtime):
    return storage.migrate(root, destination, runtime=runtime, preflight_check=lambda *_: 100)


def cleanup_links(root):
    # pytest must not recursively walk a live junction during temporary cleanup.
    for name, relative in storage.NODES:
        source = root / relative
        if source.exists() and storage.reparse(source):
            os.rmdir(source)


def test_move_keeps_exact_bytes_and_requires_explicit_cleanup(install):
    root, destination = install
    original = storage.inventory(root / 'backend/data')
    runtime = FakeRuntime(root)
    try:
        state = run(root, destination, runtime)
        assert state['stage'] == 'verified'
        assert storage.inventory(destination / 'data') == original
        assert os.path.samefile(root / 'backend/data/dense_medium.db', destination / 'data/dense_medium.db')
        assert all(Path(node['parked']).is_dir() for node in state['nodes'])
        storage.finalize(root, destination, runtime=runtime)
        assert all(Path(node['parked']).exists() for node in state['nodes'])
        storage.finalize(root, destination, runtime=runtime, confirm=True)
        assert all(not Path(node['parked']).exists() for node in state['nodes'])
        assert (destination / 'data/write_token.txt').read_text() == 'synthetic-token'
        assert (destination / 'backups/old.db').read_bytes() == b'synthetic-backup'
        assert ('verify', False) in runtime.events
    finally:
        cleanup_links(root)


def test_second_junction_failure_restores_both_original_paths(install, monkeypatch):
    root, destination = install
    create = storage.make_junction
    def failure(source, target):
        if source.name == 'backups':
            raise RuntimeError('simulated second junction failure')
        create(source, target)
    monkeypatch.setattr(storage, 'make_junction', failure)
    with pytest.raises(RuntimeError, match='second junction'):
        run(root, destination, FakeRuntime(root))
    assert not storage.reparse(root / 'backend/data')
    assert not storage.reparse(root / 'backups')
    assert (root / 'backend/data/write_token.txt').exists()
    assert json.loads((destination / 'storage-migration.json').read_text())['stage'] == 'rolled_back'


def test_late_rollback_preserves_new_d_reading(install):
    root, destination = install
    with pytest.raises(RuntimeError, match='late health failure'):
        run(root, destination, FakeRuntime(root, fail_verify=True))
    assert not storage.reparse(root / 'backend/data')
    for database in (root / 'backend/data/dense_medium.db', destination / 'data/dense_medium.db'):
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute('SELECT value FROM readings ORDER BY value').fetchall() == [(1,), (2,)]


def test_failed_sync_back_keeps_active_d_links(install, monkeypatch):
    root, destination = install
    def fail(*_):
        raise OSError('simulated full C drive')
    monkeypatch.setattr(storage, 'sync_back', fail)
    try:
        with pytest.raises(RuntimeError, match='late health failure'):
            run(root, destination, FakeRuntime(root, fail_verify=True))
        assert storage.reparse(root / 'backend/data')
        state = json.loads((destination / 'storage-migration.json').read_text())
        assert state['stage'] == 'recovery_required'
        with pytest.raises(ValueError, match='Only a verified migration'):
            storage.finalize(root, destination, runtime=FakeRuntime(root), confirm=True)
    finally:
        cleanup_links(root)


def test_nested_junction_is_rejected_before_copy(install, tmp_path):
    root, destination = install
    other = tmp_path / 'external'
    other.mkdir()
    junction = root / 'backend/data/nested'
    # The fixed helper intentionally allows only the two installed source nodes.
    command = ['powershell.exe', '-NoProfile', '-Command',
        "New-Item -ItemType Junction -Path '" + str(junction).replace("'", "''") + "' -Target '" + str(other).replace("'", "''") + "' | Out-Null"]
    subprocess.run(command, check=True, capture_output=True)
    try:
        with pytest.raises(ValueError, match='Nested reparse point'):
            storage.inventory(root / 'backend/data')
    finally:
        os.rmdir(junction)


def test_finalize_rejects_manifest_path_tampering(install):
    root, destination = install
    try:
        state = run(root, destination, FakeRuntime(root))
        state['nodes'][0]['parked'] = str(root.parent)
        storage.write_state(destination, state)
        with pytest.raises(ValueError, match='unexpected directory paths'):
            storage.finalize(root, destination, runtime=FakeRuntime(root), confirm=True)
        assert root.exists()
    finally:
        cleanup_links(root)


def test_corrupt_copy_is_not_published(install, monkeypatch):
    root, destination = install
    real_copy = storage.shutil.copytree
    def corrupt(source, target, *args, **kwargs):
        real_copy(source, target, *args, **kwargs)
        if Path(source).name == 'data':
            (Path(target) / 'write_token.txt').write_text('changed')
    monkeypatch.setattr(storage.shutil, 'copytree', corrupt)
    with pytest.raises(ValueError, match='Copied files differ'):
        run(root, destination, FakeRuntime(root))
    assert not storage.reparse(root / 'backend/data')
    assert (root / 'backend/data/write_token.txt').read_text() == 'synthetic-token'


def test_watchdog_stops_only_actual_owned_script(install, tmp_path):
    root, _ = install
    owned = root / 'backend/scripts/watchdog_server.ps1'
    owned.parent.mkdir(parents=True)
    owned.write_text('param([int]$IntervalSec=60)\nStart-Sleep -Seconds 120')
    foreign = tmp_path / 'foreign-watchdog.ps1'
    foreign.write_text('Start-Sleep -Seconds 120')
    processes = [
        subprocess.Popen(['powershell.exe', '-NoProfile', '-File', str(owned), '-IntervalSec', '42'], creationflags=subprocess.CREATE_NO_WINDOW),
        subprocess.Popen(['powershell.exe', '-NoProfile', '-File', str(foreign)], creationflags=subprocess.CREATE_NO_WINDOW),
        subprocess.Popen(['powershell.exe', '-NoProfile', '-Command', "$text='" + str(owned).replace("'", "''") + "'; Start-Sleep -Seconds 120"], creationflags=subprocess.CREATE_NO_WINDOW),
    ]
    try:
        time.sleep(1)
        result = storage.powershell(storage.PACKAGE / 'MoveStorageToD.ps1', '-InstallDir', root,
            '-StopOwnedWatchdog', capture=True)
        assert json.loads(result)['intervals'] == [42]
        processes[0].wait(timeout=5)
        assert all(process.poll() is None for process in processes[1:])
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


def test_package_hash_manifest_matches_all_helpers(tmp_path, monkeypatch):
    path = SCRIPT.parent.parent / 'package_storage_migration.py'
    spec = importlib.util.spec_from_file_location('storage_packager', path)
    packager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packager)
    archive = tmp_path / 'migration.zip'
    packager.build(archive)
    unpacked = tmp_path / 'unpacked'
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.testzip() is None
        bundle.extractall(unpacked)
    monkeypatch.setattr(storage, 'PACKAGE', unpacked)
    storage.validate_package()
    (unpacked / 'StopOwnedCollector.ps1').write_text('changed')
    with pytest.raises(ValueError, match='integrity failed'):
        storage.validate_package()


def test_relative_watchdog_refuses_before_stopping_any_process(install):
    root, _ = install
    owned = root / 'backend/scripts/watchdog_server.ps1'
    owned.parent.mkdir(parents=True)
    owned.write_text('param([int]$IntervalSec=60)\nStart-Sleep -Seconds 120')
    processes = [
        subprocess.Popen(['powershell.exe', '-NoProfile', '-File', str(owned)], creationflags=subprocess.CREATE_NO_WINDOW),
        subprocess.Popen(['powershell.exe', '-NoProfile', '-File', 'backend\\scripts\\watchdog_server.ps1'], cwd=root, creationflags=subprocess.CREATE_NO_WINDOW),
    ]
    try:
        time.sleep(1)
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', str(storage.PACKAGE / 'MoveStorageToD.ps1'), '-InstallDir', str(root),
            '-StopOwnedWatchdog'], capture_output=True, text=True, encoding='utf-8', timeout=30)
        assert result.returncode != 0
        assert 'relative-path watchdog_server.ps1 is running' in result.stderr
        assert all(process.poll() is None for process in processes)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
