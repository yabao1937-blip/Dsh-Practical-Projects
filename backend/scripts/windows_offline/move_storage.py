"""Move only production data/backups, keeping installed code and Python paths intact.

No source directory is deleted during migration. The explicit finalize command is
the sole path that removes the two verified, parked C-drive directories.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from urllib.request import urlopen

PACKAGE = Path(__file__).resolve().parent
NODES = (('data', 'backend/data'), ('backups', 'backups'))


def say(message):
    print(message, flush=True)


def absolute(path):
    # Do not resolve a junction when validating its logical installed path.
    return Path(os.path.abspath(path))


def reparse(path):
    return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def same_path(first, second):
    return os.path.normcase(str(absolute(first))) == os.path.normcase(str(absolute(second)))


def digest(path):
    result = hashlib.sha256()
    copied, reported = 0, time.monotonic()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
            copied += len(block)
            if time.monotonic() - reported >= 5:
                say('Hashing %s: %d MiB checked.' % (path.name, copied // 1048576))
                reported = time.monotonic()
    return result.hexdigest()


def inventory(path, *, hashes=True):
    """Never traverse a reparse point, including one nested inside a backup."""
    if reparse(path) or not path.is_dir():
        raise ValueError('Expected an ordinary directory: ' + str(path))
    files = {}
    for folder, directories, names in os.walk(path, followlinks=False):
        for name in directories + names:
            item = Path(folder) / name
            if reparse(item):
                raise ValueError('Nested reparse point is not supported: ' + str(item))
        for name in names:
            item = Path(folder) / name
            info = item.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('Unsupported file type: ' + str(item))
            files[item.relative_to(path).as_posix()] = (info.st_size, digest(item) if hashes else '')
    return files


def write_state(destination, state):
    temporary = destination / 'storage-migration.json.tmp'
    temporary.write_text(json.dumps(state, indent=2), encoding='utf-8')
    os.replace(temporary, destination / 'storage-migration.json')


def powershell(script, *arguments, capture=False):
    result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', str(script), *map(str, arguments)], check=True, timeout=180,
        stdout=subprocess.PIPE if capture else None, encoding='utf-8' if capture else None)
    return result.stdout if capture else None


def local_ntfs(destination):
    import ctypes
    drive = absolute(destination).anchor
    filesystem = ctypes.create_unicode_buffer(32)
    kernel = ctypes.windll.kernel32
    if kernel.GetDriveTypeW(ctypes.c_wchar_p(drive)) != 3:
        raise ValueError('The destination must be a local fixed drive.')
    if not kernel.GetVolumeInformationW(ctypes.c_wchar_p(drive), None, 0, None, None, None, filesystem, len(filesystem)):
        raise OSError('Cannot read destination volume information.')
    if filesystem.value.upper() != 'NTFS':
        raise ValueError('The destination must use NTFS directory junctions.')


def check_parents(path):
    for parent in (path, *path.parents):
        if parent.exists() and reparse(parent):
            raise ValueError('A migration parent is a reparse point: ' + str(parent))


def validate_package():
    manifest = json.loads((PACKAGE / 'storage-package.json').read_text(encoding='utf-8'))
    for name, expected in manifest['files'].items():
        if '/' in name or '\\' in name or name in ('.', '..'):
            raise ValueError('Unsafe package file name')
        if digest(PACKAGE / name) != expected:
            raise ValueError('Migration package integrity failed: ' + name)


def preflight(root, destination):
    if os.name != 'nt':
        raise ValueError('This migration requires Windows.')
    for name in ('offline-install.json', 'Start.ps1', 'backend/.venv/Scripts/python.exe'):
        if not (root / name).is_file():
            raise ValueError('Unrecognized installation: ' + name)
    check_parents(root)
    check_parents(destination)
    if destination == root or root in destination.parents or destination in root.parents:
        raise ValueError('The data destination must be outside the installation.')
    if destination.exists():
        raise ValueError('Destination already exists. Preserve it and inspect storage-migration.json; do not rerun into it.')
    local_ntfs(destination)
    total = 0
    for _, relative in NODES:
        source = root / relative
        check_parents(source)
        if not source.is_dir():
            raise ValueError('Required source directory is missing: ' + str(source))
        total += sum(size for size, _ in inventory(source, hashes=False).values())
    existing_parent = next(parent for parent in destination.parents if parent.exists())
    required = total + max(256 * 1024 ** 2, total // 20)
    if shutil.disk_usage(existing_parent).free < required:
        raise ValueError('Insufficient destination space; need at least %d MiB.' % (required // 1048576))
    return total


def checkpoint(database, *, full=False):
    with closing(sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=15)) as connection:
        if connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
            raise ValueError('Database is still in use; no directory was switched.')
        schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        if not schema:
            raise ValueError('The production database has no schema.')
        if full:
            say('Optional full database check; this may take many minutes on a large database.')
            started, reported = time.monotonic(), [time.monotonic()]
            def progress():
                now = time.monotonic()
                if now - reported[0] >= 5:
                    say('Checking database contents: %ds elapsed.' % (now - started))
                    reported[0] = now
                return int(now - started > 3600)
            connection.set_progress_handler(progress, 10000)
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Database content check failed.')
        return hashlib.sha256(json.dumps(schema).encode()).hexdigest()


def readonly_schema(database):
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)) as connection:
        schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        return hashlib.sha256(json.dumps(schema).encode()).hexdigest()


def copy_verified(source, target):
    say('Copying ' + str(source) + ' to ' + str(target))
    shutil.copytree(source, target, copy_function=copy_file)
    say('Checking every copied file (streaming SHA-256): ' + str(target))
    first, second = inventory(source), inventory(target)
    if first != second:
        raise ValueError('Copied files differ; the source directory was preserved.')
    return dict(files=len(first), bytes=sum(size for size, _ in first.values()))


def copy_file(source, target):
    source, target = Path(source), Path(target)
    copied, reported = 0, time.monotonic()
    with source.open('rb') as incoming, target.open('wb') as outgoing:
        for block in iter(lambda: incoming.read(1024 * 1024), b''):
            outgoing.write(block)
            copied += len(block)
            if time.monotonic() - reported >= 5:
                say('Copying %s: %d MiB copied.' % (source.name, copied // 1048576))
                reported = time.monotonic()
    shutil.copystat(source, target)
    return str(target)


def make_junction(source, target):
    root = source.parent.parent if source.name == 'data' else source.parent
    powershell(PACKAGE / 'MoveStorageToD.ps1', '-InstallDir', root,
        '-CreateJunction', '-SourcePath', source, '-TargetPath', target)
    if not reparse(source) or not same_path(source.resolve(), target):
        raise ValueError('Directory junction verification failed: ' + str(source))


def remove_junction(source, target):
    if not reparse(source) or not same_path(source.resolve(), target):
        raise ValueError('Refusing to remove an unexpected junction: ' + str(source))
    # rmdir without recursion removes the junction itself, never its D-drive target.
    os.rmdir(source)


class Runtime:
    def __init__(self, root):
        import apply_update
        self.update, self.root, self.watchdogs = apply_update, root, []
        self.configuration = apply_update.collector_configuration(root)
        self.before = apply_update.collector_status(root, self.configuration['source_id'])

    def stop(self, *, watchdog=True):
        if watchdog:
            result = powershell(PACKAGE / 'MoveStorageToD.ps1', '-InstallDir', self.root,
                '-StopOwnedWatchdog', capture=True)
            self.watchdogs = json.loads(result)['intervals']
        powershell(PACKAGE / 'StopOwnedWeb.ps1', '-InstallDir', self.root)
        powershell(PACKAGE / 'StopOwnedCollector.ps1', '-InstallDir', self.root)
        self.update.wait_collector_lease(self.root, self.configuration, lambda stage, message: say(message))

    def start(self):
        self.update.restart(self.root)

    def verify(self, *, restarted=True):
        with urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=15) as response:
            if json.load(response).get('status') != 'ok':
                raise ValueError('The web service is not ready; old C copies were retained.')
        before = dict(self.before)
        if not restarted:
            before['owner'] = None
        self.update.collector_ready(self.root, self.configuration, before, lambda stage, message: say(message))

    def restore_watchdog(self):
        if self.watchdogs:
            powershell(PACKAGE / 'MoveStorageToD.ps1', '-InstallDir', self.root,
                '-RestartOwnedWatchdog', '-WatchdogInterval', min(self.watchdogs))
            self.watchdogs = []


def validate_state(root, destination, state):
    if state.get('format') != 1 or not same_path(state.get('install_dir', ''), root) or not same_path(state.get('destination_root', ''), destination):
        raise ValueError('Migration record does not match this installation and destination.')
    stamp = state.get('stamp', '')
    if not stamp or any(character not in '0123456789-' for character in stamp):
        raise ValueError('Invalid migration timestamp.')
    if len(state.get('nodes', [])) != 2:
        raise ValueError('Invalid migration directory count.')
    for node, (name, relative) in zip(state['nodes'], NODES):
        if (node.get('name') != name or not same_path(node['source'], root / relative)
                or not same_path(node['target'], destination / name)
                or not same_path(node['parked'], (root / relative).with_name(name + '.before-D-' + stamp))):
            raise ValueError('Migration record contains unexpected directory paths.')


def verify_links(root, destination, state):
    check_parents(root)
    check_parents(destination)
    validate_state(root, destination, state)
    for node in state['nodes']:
        source, target = Path(node['source']), Path(node['target'])
        check_parents(source.parent)
        if not reparse(source) or not same_path(source.resolve(), target) or reparse(target):
            raise ValueError('Production storage is not linked to the expected D-drive directory.')
    database = root / 'backend/data/dense_medium.db'
    expected = destination / 'data/dense_medium.db'
    if not os.path.samefile(database, expected) or not same_path(database.resolve(), expected):
        raise ValueError('The database does not resolve to the expected D-drive file.')
    readonly_schema(expected)
    spool = root / 'backend/data/telemetry-spool'
    if spool.exists() and not same_path(spool.resolve(), destination / 'data/telemetry-spool'):
        raise ValueError('Collector spool does not resolve to D.')


def sync_back(source, target):
    """Restore new D-drive writes before a late rollback; keep D authoritative on failure."""
    current, previous = inventory(source), inventory(target)
    for relative in current:
        output = target / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        copy_file(source / relative, output)
    for relative in previous.keys() - current.keys():
        # Only obsolete files in the explicitly parked directory; the D copy is retained.
        (target / relative).unlink()
    if inventory(target) != current:
        raise ValueError('Rollback copy verification failed; keep the D-drive junctions.')


def rollback(state, root, destination, runtime, started):
    if started:
        runtime.stop(watchdog=False)
        checkpoint(destination / 'data/dense_medium.db')
        for node in state['nodes']:
            if node.get('linked'):
                sync_back(Path(node['target']), Path(node['parked']))
    for node in reversed(state['nodes']):
        source, target, parked = map(Path, (node['source'], node['target'], node['parked']))
        if source.exists() and reparse(source):
            remove_junction(source, target)
        if node.get('parked_done'):
            parked.rename(source)
    runtime.start()
    runtime.restore_watchdog()
    state['stage'] = 'rolled_back'
    write_state(destination, state)


def migrate(root, destination, *, full=False, runtime=None, preflight_check=preflight):
    total = preflight_check(root, destination)
    runtime = runtime or Runtime(root)
    destination.mkdir(parents=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    state = dict(format=1, stage='prepared', stamp=stamp, install_dir=str(root),
        destination_root=str(destination), source_bytes=total, nodes=[])
    for name, relative in NODES:
        source = root / relative
        state['nodes'].append(dict(name=name, source=str(source), target=str(destination / name),
            parked=str(source.with_name(name + '.before-D-' + stamp)), parked_done=False, linked=False))
    write_state(destination, state)
    stopped, started = False, False
    try:
        say('Stopping only this installation. Do not close this window or start another update.')
        stopped = True  # stop can fail partway; recovery must still restore the web service.
        runtime.stop()
        schema = checkpoint(root / 'backend/data/dense_medium.db', full=full)
        for node in state['nodes']:
            node['copied'] = copy_verified(Path(node['source']), Path(node['target']))
        if readonly_schema(destination / 'data/dense_medium.db') != schema:
            raise ValueError('Copied database schema differs.')
        for node in state['nodes']:
            source, parked, target = map(Path, (node['source'], node['parked'], node['target']))
            source.rename(parked)
            node['parked_done'] = True
            write_state(destination, state)
            make_junction(source, target)
            node['linked'] = True
            write_state(destination, state)
        state['stage'] = 'linked'
        write_state(destination, state)
        verify_links(root, destination, state)
        started = True
        runtime.start()
        runtime.verify()
        state['stage'] = 'verified'
        state['verified_at'] = datetime.now(timezone.utc).isoformat()
        write_state(destination, state)
        runtime.restore_watchdog()
        say('Storage migration verified. Database, spool and backups now use D; application code remains at its original C path.')
        say('IMPORTANT: old C-drive copies were retained and STILL USE SPACE. FinalizeStorageMove.ps1 previews cleanup; -ConfirmRemoval is required to release that space.')
        return state
    except Exception:
        if stopped:
            try:
                rollback(state, root, destination, runtime, started)
                say('Migration failed; original directory paths and service were restored. D-drive copies were retained.')
            except Exception as recovery:
                state['stage'] = 'recovery_required'
                state['recovery_error'] = type(recovery).__name__
                write_state(destination, state)
                try:
                    runtime.start()
                    runtime.restore_watchdog()
                except Exception:
                    pass
                say('Recovery requires inspection. D data was retained; do not delete either copy or run finalize.')
        raise


def finalize(root, destination, *, confirm=False, runtime=None):
    check_parents(root)
    check_parents(destination)
    state = json.loads((destination / 'storage-migration.json').read_text(encoding='utf-8'))
    if state.get('stage') not in ('verified', 'finalizing'):
        raise ValueError('Only a verified migration may release its old C-drive copies.')
    verify_links(root, destination, state)
    runtime = runtime or Runtime(root)
    runtime.verify(restarted=False)  # Require fresh samples again, not a changed process owner.
    parked_nodes = []
    for node in state['nodes']:
        parked = Path(node['parked'])
        if node.get('removed'):
            if parked.exists():
                raise ValueError('A previously removed directory has reappeared; inspect manually.')
            continue
        files = inventory(parked, hashes=False)
        size = sum(item[0] for item in files.values())
        say('Old C-drive copy: %s (%d MiB)' % (parked, size // 1048576))
        parked_nodes.append((node, parked))
    if not confirm:
        say('PREVIEW ONLY. No files removed. Run FinalizeStorageMove.ps1 -ConfirmRemoval to release only the listed old C-drive copies.')
        return
    state['stage'] = 'finalizing'
    write_state(destination, state)
    for node, parked in parked_nodes:
        # Exact parked names were checked against the two fixed installed paths;
        # inventory rejected every reparse point before any recursive removal.
        if reparse(parked) or not same_path(parked.parent, Path(node['source']).parent):
            raise ValueError('Refusing an unexpected cleanup directory.')
        shutil.rmtree(parked)
        node['removed'] = True
        write_state(destination, state)
    state['stage'] = 'finalized'
    state['finalized_at'] = datetime.now(timezone.utc).isoformat()
    write_state(destination, state)
    say('Old C-drive copies removed. Active D-drive production storage and historical backups were preserved.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-dir', type=Path, required=True)
    parser.add_argument('--destination-root', type=Path, default=Path('D:/DshDensityControlData'))
    parser.add_argument('--full-check', action='store_true')
    parser.add_argument('--finalize', action='store_true')
    parser.add_argument('--confirm-removal', action='store_true')
    args = parser.parse_args()
    try:
        validate_package()
        root, destination = absolute(args.install_dir), absolute(args.destination_root)
        if args.finalize:
            finalize(root, destination, confirm=args.confirm_removal)
        else:
            if args.confirm_removal:
                raise ValueError('--confirm-removal is only valid with --finalize')
            migrate(root, destination, full=args.full_check)
        return 0
    except Exception as exc:
        say('STORAGE OPERATION FAILED: ' + str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
