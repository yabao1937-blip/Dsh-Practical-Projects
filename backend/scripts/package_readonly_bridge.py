"""Build the narrow read-only transport installer from one explicit Git commit."""
import argparse
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = ROOT / 'backend/scripts/windows_offline'
spec = importlib.util.spec_from_file_location('readonly_bridge_installer', SCRIPT_DIR / 'enable_readonly_bridge.py')
installer = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(SCRIPT_DIR))
try:
    spec.loader.exec_module(installer)
finally:
    sys.path.pop(0)


def git_bytes(revision, name, *, root=ROOT):
    return subprocess.check_output(['git', 'show', revision + ':' + name], cwd=root, stderr=subprocess.DEVNULL)


def previous_hashes(output):
    """Prefer the newest installed hotfix, then its full parent, then original."""
    result = {}
    for archive_path in (output / 'extras/ProjectUpdate.zip', output / 'extras/ProjectUpdate-c9a7bb9.zip'):
        if not archive_path.is_file():
            continue
        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read('update.json'))
            for name, info in manifest['files'].items():
                result.setdefault(name, info['sha256'])
    latest = json.loads((output / 'latest.json').read_text(encoding='utf-8-sig'))
    baseline = json.loads((Path(latest['stage']) / 'manifest.json').read_text(encoding='utf-8-sig'))
    for name, digest in baseline['files'].items():
        if name.startswith('project/'):
            result.setdefault(name[len('project/'):], digest)
    return result


def build(output, revision, access_config, *, root=ROOT):
    output, root = Path(output).resolve(), Path(root).resolve()
    access = installer.read_access(access_config)
    # The installer probes from loopback before the client is allowed to connect.
    if '127.0.0.1' not in access['clients']:
        raise ValueError('Access configuration must permit the local verification probe')
    revision = subprocess.check_output(['git', 'rev-parse', '--verify', revision + '^{commit}'],
        cwd=root, text=True, stderr=subprocess.DEVNULL).strip()
    previous = previous_hashes(output)
    files, payload = {}, {}
    for name in sorted(installer.RUNTIME_FILES):
        data = git_bytes(revision, name, root=root)
        files[name] = {'sha256': hashlib.sha256(data).hexdigest(), 'previous_sha256': previous.get(name)}
        payload['payload/' + name] = data
    manifest = {'format': 1, 'profile': installer.PROFILE, 'revision': revision,
        'created_at': datetime.now().astimezone().isoformat(), 'files': files}
    settings_path = output / 'transfer-settings.json'
    if settings_path.is_file():
        settings = json.loads(settings_path.read_text(encoding='utf-8-sig'))
        manifest['report_url'] = f'http://{settings["bind"]}:{settings["port"]}/{settings["token"]}/status'
    target = output / 'extras/ReadOnlyBridge.zip'
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('readonly-update.json', json.dumps(manifest, indent=2))
        archive.writestr('access-config.json', json.dumps(access, indent=2))
        for name, data in payload.items():
            archive.writestr(name, data)
        for name in ('enable_readonly_bridge.py', 'EnableReadOnlyTest.cmd', 'apply_update.py', 'StopOwnedWeb.ps1'):
            archive.writestr(name, git_bytes(revision, 'backend/scripts/windows_offline/' + name, root=root))
    print(json.dumps({'package': str(target), 'bytes': target.stat().st_size,
                      'files': len(files), 'revision': revision}))
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/offline-win10-x64')
    parser.add_argument('--revision', required=True)
    parser.add_argument('--access-config', type=Path, required=True)
    args = parser.parse_args()
    build(args.output, args.revision, args.access_config)
