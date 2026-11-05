"""Build a self-contained Windows bundle from this verified CPython installation.

No live venv launchers are copied. Packages become RECORD-verified wheels and a
new venv is created at the destination. The live SQLite is read through a fixed
online-backup snapshot, never copied beside an uncheckpointed WAL.
"""
import argparse
import base64
import csv
from datetime import datetime
import hashlib
import importlib.metadata as metadata
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
from email.parser import Parser
from zipfile import ZipFile, ZIP_DEFLATED

from packaging.tags import sys_tags
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'backend'))
TEMPLATES = Path(__file__).with_name('windows_offline')


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def pins(path):
    result = {}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.split('#', 1)[0].strip()
        if line.startswith('-r '):
            result.update(pins(path.parent / line[3:].strip()))
        elif line:
            name, version = line.split('==')
            result[canonicalize_name(name)] = version
    return result


def make_wheels(destination):
    destination.mkdir(parents=True, exist_ok=True)
    site = Path(sysconfig.get_path('purelib')).resolve()
    distributions = {canonicalize_name(d.metadata['Name']): d for d in metadata.distributions(path=[str(site)])}
    wanted = pins(ROOT / 'backend/requirements-kepserver.lock.txt')
    report = []
    for name, version in sorted(wanted.items()):
        dist = distributions[name]
        if dist.version != version:
            raise RuntimeError(f'{name}: installed {dist.version}, expected {version}')
        distinfo = next(Path(p).parent for p in dist.files if str(p).endswith('.dist-info/WHEEL'))
        wheel_meta = Parser().parsestr((site/distinfo/'WHEEL').read_text(encoding='utf-8'))
        accepted = set(wheel_meta.get_all('Tag', []))
        tag = next((str(t) for t in sys_tags() if str(t) in accepted), None)
        if not tag:
            raise RuntimeError(f'{name}: no compatible installed wheel tag')
        wheel = destination / f'{re.sub(r"[-_.]+", "_", name)}-{version}-{tag}.whl'
        temporary = wheel.with_suffix('.partial')
        entries = []
        console = {e.name for e in dist.entry_points if e.group in ('console_scripts', 'gui_scripts')}
        with ZipFile(temporary, 'w', ZIP_DEFLATED, compresslevel=1) as output:
            rows = csv.reader(io.StringIO((site/distinfo/'RECORD').read_text(encoding='utf-8')))
            for relative, digest, size in rows:
                path = (site/relative).resolve()
                if not path.is_relative_to(site):
                    # pip regenerates console launchers with the NEW Python path.
                    stem = path.name.removesuffix('.exe').removesuffix('-script.py').removesuffix('.py')
                    if path.parent == site.parents[1]/'Scripts' and stem in console:
                        continue
                    external = path.relative_to(site.parents[1]) if path.is_relative_to(site.parents[1]) else None
                    if (external and len(external.parts) >= 5 and external.parts[0].lower() == 'include'
                            and external.parts[1:3] == ('site', 'python3.11')
                            and canonicalize_name(external.parts[3]) == name):
                        relative = distinfo.name.removesuffix('.dist-info')+'.data/headers/'+Path(*external.parts[4:]).as_posix()
                    else:
                        raise RuntimeError(f'{name}: unexpected external RECORD path {relative}')
                else:
                    relative = path.relative_to(site).as_posix()
                if path.suffix == '.pyc' or path.name in ('RECORD', 'INSTALLER', 'REQUESTED', 'direct_url.json'):
                    continue
                data = path.read_bytes()
                if digest:
                    algorithm, expected = digest.split('=', 1)
                    actual = base64.urlsafe_b64encode(hashlib.new(algorithm, data).digest()).rstrip(b'=').decode()
                    if actual != expected:
                        raise RuntimeError(f'{name}: installed RECORD hash mismatch for {relative}')
                if size and len(data) != int(size):
                    raise RuntimeError(f'{name}: installed RECORD size mismatch for {relative}')
                output.writestr(relative, data)
                hashed = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
                entries.append((relative, 'sha256='+hashed, str(len(data))))
            record = (distinfo/'RECORD').as_posix()
            buffer = io.StringIO(newline='')
            csv.writer(buffer, lineterminator='\n').writerows(entries+[(record, '', '')])
            output.writestr(record, buffer.getvalue().encode())
        temporary.replace(wheel)
        report.append(dict(name=name, version=version, wheel=wheel.name, sha256=sha(wheel), files=len(entries)))
        print(f'wheel {name} {version}', flush=True)
    return report


def copy_runtime(target):
    """An uninstalled CPython prefix, WITHOUT machine-specific venv metadata."""
    base = Path(sys.base_prefix)
    target.mkdir(parents=True)
    for name in ('python.exe', 'pythonw.exe', 'python3.dll', 'python311.dll',
                 'vcruntime140.dll', 'vcruntime140_1.dll', 'LICENSE.txt'):
        path = base/name
        if path.exists():
            shutil.copy2(path, target/name)
    for name in ('DLLs', 'Lib'):
        shutil.copytree(base/name, target/name,
            ignore=shutil.ignore_patterns('site-packages', '__pycache__', 'test', 'tests', 'idlelib', 'tkinter'))


def database_snapshot(target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect((ROOT/'backend/data/dense_medium.db').as_uri()+'?mode=ro', uri=True) as source:
        source.execute('BEGIN')
        source.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
        with sqlite3.connect(target) as destination:
            source.backup(destination, pages=2048, sleep=.05)
            if destination.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise RuntimeError('Snapshot integrity check failed')
        source.rollback()
    from point_metadata import copy_catalog
    copy_catalog(ROOT/'backend/data/dense_medium.db', target)


def verify_runtime(output, wheelhouse, packages):
    """Actually install without an index into a new, relocated CPython prefix."""
    qa = output/('verification-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    qa.mkdir()
    copy_runtime(qa/'runtime')
    base = qa/'runtime/python.exe'
    python = qa/'environment/Scripts/python.exe'
    env = {k:v for k,v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
    env.update(PYTHONUTF8='1', PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1')
    commands = [
        [str(base), '-I', '-m', 'venv', str(qa/'environment')],
        [str(python), '-m', 'pip', 'install', '--no-index', '--find-links', str(wheelhouse),
            '-r', str(ROOT/'backend/requirements-kepserver.lock.txt')],
        [str(python), '-m', 'pip', 'check'],
        [str(python), '-I', '-c',
            'import asyncua,fastapi,uvicorn,sqlalchemy,openpyxl,numpy,scipy.linalg,sklearn,cryptography,ssl,sqlite3;'
            'import sys;print("ISOLATED_IMPORTS_OK",sys.version,sys.executable)'],
    ]
    with (qa/'verification.log').open('w', encoding='utf-8') as log:
        for command in commands:
            result = subprocess.run(command, cwd=qa, env=env, stdout=log, stderr=subprocess.STDOUT)
            if result.returncode:
                raise RuntimeError(f'Offline verification failed; see {qa / "verification.log"}')
    report = dict(passed=True, python=sys.version.split()[0], packages=packages, directory=str(qa),
        checks=['relocated-cpython', 'fresh-venv', 'no-index-install', 'pip-check', 'native-library-imports'])
    (output/'verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('Clean relocated runtime: offline installation and native imports passed.', flush=True)


def build(output, wheelhouse, packages):
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    stage = output/('bundle-'+stamp)
    stage.mkdir(parents=True)
    project = stage/'project'
    tracked = subprocess.check_output(['git', 'ls-files', '-z', 'backend', 'frontend'], cwd=ROOT).decode().split('\0')
    # Also include the packaging scripts during their first verification build.
    tracked += [str(p.relative_to(ROOT)).replace('\\', '/') for p in TEMPLATES.glob('*')]
    tracked.append(Path(__file__).relative_to(ROOT).as_posix())
    for relative in sorted(set(tracked)-{''}):
        path = ROOT/relative
        if not path.is_file():
            continue
        destination = project/relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    (stage/'wheels').mkdir()
    for package in packages:
        shutil.copy2(wheelhouse/package['wheel'], stage/'wheels'/package['wheel'])
    copy_runtime(stage/'runtime')
    print('Backing up SQLite through a fixed read snapshot...', flush=True)
    database_snapshot(stage/'snapshot/dense_medium.db')
    config = ROOT/'backend/config/kepserver.local.json'
    if config.exists():
        (stage/'configuration').mkdir()
        shutil.copy2(config, stage/'configuration/kepserver.local.json')
        # 2026-11-05 安全审计：certs/ 含 OPC 客户端私钥，绝不进分发包。
        # 主机侧安全模式（Basic256Sha256）启用时，证书应通过单独的受控渠道放置，
        # 而不是随安装包广播。此处显式跳过并大声提示。
        certs = config.parent/'certs'
        if certs.exists():
            skipped = [p for p in certs.rglob('*') if p.is_file()]
            print(f'WARNING: OPC certs directory ({len(skipped)} files) EXCLUDED from the '
                  f'bundle — client private keys must be distributed via a separate controlled '
                  f'channel, never inside the install zip.', flush=True)
    for name in ('Install.cmd', 'Install.ps1', 'Start.ps1'):
        shutil.copy2(TEMPLATES/name, stage/name)
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    transfer_file = output/'transfer-settings.json'
    if not transfer_file.exists():
        transfer_file.write_text(json.dumps(dict(bind='10.255.2.7', client='10.255.2.8', port=8765,
            token=secrets.token_urlsafe(18))), encoding='utf-8')
    transfer = json.loads(transfer_file.read_text(encoding='utf-8'))
    manifest = dict(format=1, revision=revision, created_at=datetime.now().astimezone().isoformat(),
        python=sys.version.split()[0], architecture='win_amd64',
        report_url=f'http://{transfer["bind"]}:{transfer["port"]}/{transfer["token"]}/status',
        dependency_origin='Repacked from RECORD-verified installed distributions; not original vendor wheel downloads',
        certs_excluded=True,  # 2026-11-05: OPC certs/ (含私钥) 永不打包，走独立受控渠道
        packages=packages, files={})
    for path in sorted(stage.rglob('*')):
        if path.is_file():
            manifest['files'][path.relative_to(stage).as_posix()] = sha(path)
    (stage/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    archive = output/('DshDensityControl-Win10-x64-'+stamp+'.zip')
    with ZipFile(archive, 'w', ZIP_DEFLATED, compresslevel=1) as zipfile:
        for path in sorted(stage.rglob('*')):
            if path.is_file():
                zipfile.write(path, path.relative_to(stage).as_posix())
    report = dict(bundle=str(archive), stage=str(stage), sha256=sha(archive), bytes=archive.stat().st_size,
        packages=len(packages), revision=revision)
    (output/'latest.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


def refresh_stage(output, stage):
    """Refresh packaging scripts after verification without changing the DB snapshot."""
    stage = stage.resolve()
    if not stage.is_relative_to(output) or not (stage/'manifest.json').is_file():
        raise ValueError('Refresh requires an existing stage inside the output directory')
    for name in ('Install.cmd', 'Install.ps1', 'Start.ps1'):
        shutil.copy2(TEMPLATES/name, stage/name)
        shutil.copy2(TEMPLATES/name, stage/'project/backend/scripts/windows_offline'/name)
    for name in ('package_windows_offline.py', 'offline_transfer.py'):
        shutil.copy2(Path(__file__).with_name(name), stage/'project/backend/scripts'/name)
    manifest = json.loads((stage/'manifest.json').read_text(encoding='utf-8'))
    manifest['revision'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    manifest['files'] = {path.relative_to(stage).as_posix():sha(path)
        for path in sorted(stage.rglob('*')) if path.is_file() and path != stage/'manifest.json'}
    (stage/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    archive = output/('DshDensityControl-Win10-x64-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.zip')
    with ZipFile(archive, 'w', ZIP_DEFLATED, compresslevel=1) as zipfile:
        for path in sorted(stage.rglob('*')):
            if path.is_file():
                zipfile.write(path, path.relative_to(stage).as_posix())
    report = dict(bundle=str(archive), stage=str(stage), sha256=sha(archive), bytes=archive.stat().st_size,
        packages=len(manifest['packages']), revision=manifest['revision'])
    (output/'latest.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT/'outputs/offline-win10-x64')
    parser.add_argument('--wheels-only', action='store_true')
    parser.add_argument('--refresh-stage', type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if args.refresh_stage:
        refresh_stage(output, args.refresh_stage)
        return
    wheelhouse = output/'wheels'
    packages = make_wheels(wheelhouse)
    output.mkdir(parents=True, exist_ok=True)
    (output/'wheels-provenance.json').write_text(json.dumps(packages, indent=2), encoding='utf-8')
    if not args.wheels_only:
        verify_runtime(output, wheelhouse, packages)
        build(output, wheelhouse, packages)


if __name__ == '__main__':
    main()
