"""Build the standalone, data-preserving Windows storage migration ZIP."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
FILES = ('MoveStorageToD.cmd', 'MoveStorageToD.ps1', 'FinalizeStorageMove.ps1',
    'move_storage.py', 'StopOwnedWeb.ps1', 'StopOwnedCollector.ps1', 'apply_update.py')


def build(destination):
    directory = ROOT / 'backend/scripts/windows_offline'
    payload = {name: (directory / name).read_bytes() for name in FILES}
    manifest = dict(format=1, files={name: hashlib.sha256(data).hexdigest() for name, data in payload.items()})
    readme = '''Storage migration (Windows, offline)\n\n1. Extract the COMPLETE ZIP into a NEW folder.\n2. Run MoveStorageToD.cmd on the production host. Do not run other update/start scripts while it runs. Close any manually opened watchdog/update window first. An ambiguous relative-path watchdog_server.ps1 causes a safe refusal before any watchdog is stopped.\n3. The original C installation remains. backend/data and root backups are copied to D:/DshDensityControlData and replaced with junctions.\n4. Old C copies remain until successful verification. This first step redirects future growth but DOES NOT yet free the old C space.\n5. Run powershell -NoProfile -ExecutionPolicy Bypass -File .\\FinalizeStorageMove.ps1 to preview the exact old directories after live D storage and fresh collection are verified.\n6. To release ONLY those parked C copies, run the same command with -ConfirmRemoval. Active D data and copied historical backups remain.\n\nThe default does not scan a multi-GB database with quick_check; it checkpoints after stopping writers, verifies every copied byte with SHA-256 and checks the copied schema. Optional -FullCheck is available on MoveStorageToD.ps1 and may take a long time.\n\nFailure before startup restores C directly. Failure after startup copies any new D writes back before restoring C. If that cannot be verified, both copies remain and the script reports recovery_required; do not delete either directory. Do not force-close the window or power off during migration; after such an interruption inspect storage-migration.json and recover the saved paths manually before starting services.\n\nDo not manually copy a live SQLite database, move only its .db while ignoring WAL, delete junction targets, or move the whole Python installation.\n'''
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in payload.items():
            archive.writestr(name, data)
        archive.writestr('storage-package.json', json.dumps(manifest, indent=2))
        archive.writestr('README.txt', readme)
    print(json.dumps(dict(package=str(destination), bytes=destination.stat().st_size, files=len(payload))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/offline-win10-x64/extras/MoveStorageToD.zip')
    build(parser.parse_args().output)
