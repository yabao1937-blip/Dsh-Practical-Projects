"""Install only the optional read-only transport; never open or restore a DB."""
import argparse
from datetime import datetime
import hashlib
import ipaddress
import json
from pathlib import Path
import shutil
import time
from urllib.request import Request, urlopen

from apply_update import clear_bytecode, powershell


PROFILE = 'readonly-test-bridge-v1'
RUNTIME_FILES = frozenset({
    'backend/app/remote_readonly/server.py',
    'backend/app/remote_readonly/wire.py',
    'backend/remote_mode.py',
    'backend/app/config.py',
    'backend/app/database.py',
    'backend/app/auth.py',
    'backend/app/main.py',
    'backend/serve.py',
})
ACCESS_PATH = 'backend/config/readonly-access.local.json'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_path(root, name):
    if name not in RUNTIME_FILES:
        raise ValueError('Source path is outside the read-only bridge profile')
    root = Path(root).resolve()
    lexical = root / name
    target = lexical.resolve()
    if not target.is_relative_to(root) or target != lexical:
        raise ValueError('Source path escapes installation')
    return target


def access_path(root):
    root = Path(root).resolve()
    lexical = root / ACCESS_PATH
    target = lexical.resolve()
    if not target.is_relative_to(root) or target != lexical:
        raise ValueError('Access configuration escapes installation')
    return target


def read_access(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict) or set(data) != {'token', 'clients'}:
            raise ValueError()
        token, clients = data['token'], data['clients']
        if (not isinstance(token, str) or len(token) < 32 or token.strip() != token
                or any(c in token for c in '\r\n')):
            raise ValueError()
        if not isinstance(clients, list) or not clients or len(clients) > 16:
            raise ValueError()
        if any(not isinstance(client, str) or str(ipaddress.ip_address(client)) != client
               for client in clients):
            raise ValueError()
        if len(set(clients)) != len(clients):
            raise ValueError()
        return data
    except (OSError, ValueError, TypeError, KeyError):
        # Never echo the source document, credential, or JSON exception contents.
        raise ValueError('Invalid dedicated read-only access configuration') from None


def restart(root):
    # The installed root Start.ps1 also invokes the collector launcher. This
    # profile deliberately starts only the already installed Web entry point.
    powershell(root / 'backend/scripts/start_server.ps1', '-NoBrowser', cwd=root)
    for _ in range(30):
        try:
            with urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3) as response:
                if json.load(response).get('status') == 'ok':
                    return
        except (OSError, ValueError, TypeError):
            pass
        time.sleep(1)
    raise RuntimeError('Updated Web service did not become ready')


def verify(package, root, manifest, access):
    if not (root / 'offline-install.json').is_file() or not (root / 'Start.ps1').is_file():
        raise ValueError('Unrecognized installed project')
    if manifest.get('profile') != PROFILE or set(manifest.get('files', {})) != RUNTIME_FILES:
        raise ValueError('Invalid read-only bridge source profile')
    if access_path(root).exists():
        raise ValueError('Read-only access configuration already exists; nothing changed')
    for name, info in manifest['files'].items():
        payload, target = source_path(package / 'payload', name), source_path(root, name)
        if not payload.is_file() or digest(payload) != info.get('sha256'):
            raise ValueError('Damaged read-only bridge payload: ' + name)
        previous = info.get('previous_sha256')
        if target.exists():
            if not target.is_file() or previous is None or digest(target) != previous:
                raise ValueError('Installed source differs from verified baseline: ' + name)
        elif previous is not None:
            raise ValueError('Installed baseline source is missing: ' + name)
    # Validation also applies to the programmatic entry point used by tests.
    if not isinstance(access, dict) or set(access) != {'token', 'clients'}:
        raise ValueError('Invalid dedicated read-only access configuration')


def check_bridge(root, access):
    """Validate the local transport without returning production rows."""
    headers = {'X-DMCS-Read-Token': access['token']}
    identifier = None
    base = 'http://127.0.0.1:8000/api/v1/readonly-db/transactions'
    try:
        with urlopen(Request(base, data=b'', method='POST', headers=headers), timeout=5) as response:
            identifier = json.load(response)['transaction_id']
        with urlopen(Request(base + '/' + identifier + '/queries',
                data=b'{"sql":"SELECT 1","parameters":[]}', method='POST',
                headers={**headers, 'Content-Type': 'application/json'}), timeout=5) as response:
            if json.load(response).get('rows') != [[1]]:
                raise RuntimeError('Read-only bridge probe failed')
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError('Read-only bridge probe failed') from None
    finally:
        if identifier is not None:
            try:
                with urlopen(Request(base + '/' + identifier, method='DELETE', headers=headers), timeout=5):
                    pass
            except OSError:
                pass


def apply(package, root, manifest, access_file, report=lambda stage, message: None):
    package, root = Path(package).resolve(), Path(root).resolve()
    access = read_access(access_file)
    verify(package, root, manifest, access)
    backup = root / 'backups' / ('readonly-bridge-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir(parents=True)
    original = {}
    for name in sorted(RUNTIME_FILES):
        target = source_path(root, name)
        original[name] = target.is_file()
        if target.is_file():
            saved = backup / 'source' / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
    (backup / 'files.json').write_text(json.dumps(original, indent=2), encoding='utf-8')
    report('copying', 'Source backup complete. Database, models, collector and other configuration are retained.')
    stopped = False
    created_access = False
    try:
        powershell(package / 'StopOwnedWeb.ps1', '-InstallDir', str(root), cwd=root)
        stopped = True
        # Refuse changes made while the source backup or Web stop was in progress.
        verify(package, root, manifest, access)
        for name in sorted(RUNTIME_FILES):
            target = source_path(root, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + '.readonly-update.tmp')
            temporary_created = False
            try:
                with temporary.open('xb') as stream:
                    temporary_created = True
                    stream.write(source_path(package / 'payload', name).read_bytes())
                temporary.replace(target)
            finally:
                if temporary_created:
                    temporary.unlink(missing_ok=True)
            clear_bytecode(target)
        target = access_path(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('x', encoding='utf-8') as stream:
            created_access = True
            json.dump(access, stream, indent=2)
        report('starting', 'Starting this installation Web service; verifying a bounded read-only probe.')
        restart(root)
        check_bridge(root, access)
    except BaseException:
        if stopped:
            report('rollback', 'Restoring source and removing new access configuration. Production database is retained.')
            powershell(package / 'StopOwnedWeb.ps1', '-InstallDir', str(root), cwd=root)
            for name, existed in original.items():
                target = source_path(root, name)
                if existed:
                    shutil.copy2(backup / 'source' / name, target)
                else:
                    target.unlink(missing_ok=True)
                clear_bytecode(target)
            if created_access:
                access_path(root).unlink(missing_ok=True)
            restart(root)
        raise
    (root / 'readonly-bridge-update.json').write_text(json.dumps({
        'revision': manifest['revision'], 'profile': PROFILE,
        'installed_at': datetime.now().astimezone().isoformat(),
        'backup': str(backup), 'database_snapshot': False,
    }, indent=2), encoding='utf-8')
    return backup


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--install-dir', type=Path, required=True)
    parser.add_argument('--access-config', type=Path, required=True)
    args = parser.parse_args()
    package = Path(__file__).resolve().parent
    manifest = json.loads((package / 'readonly-update.json').read_text(encoding='utf-8'))

    def report(stage, message):
        print(stage + ': ' + message, flush=True)
        url = manifest.get('report_url')
        if not url:
            return
        try:
            data = json.dumps(dict(stage=stage, message=message, revision=manifest['revision'],
                healthy=stage == 'installed', at=datetime.now().astimezone().isoformat())).encode()
            with urlopen(Request(url, data=data, headers={'Content-Type': 'application/json'}), timeout=4):
                pass
        except OSError:
            pass

    try:
        report('verifying', 'Verifying dedicated read-only bridge sources and access configuration.')
        apply(package, args.install_dir, manifest, args.access_config, report)
        report('installed', 'Read-only test transport enabled. Production database and collector were retained.')
        return 0
    except Exception as error:
        # Known validation text contains file names, never values. Runtime
        # exceptions are represented by type only so tokens cannot reach reports.
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        report('failed', message)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
