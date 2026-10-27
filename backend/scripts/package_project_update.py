"""Build a source-only delta against a verified installed offline bundle."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def revision_hashes(revision, *, root=ROOT):
    """Hash committed bytes, without trusting files in a developer checkout."""
    from windows_offline.apply_update import allowed
    commit = subprocess.check_output(
        ['git','rev-parse','--verify',revision+'^{commit}'],cwd=root,text=True).strip()
    entries = subprocess.check_output(['git','ls-tree','-rz',commit],cwd=root).split(b'\0')
    blobs = {}
    for entry in entries:
        if not entry: continue
        metadata, raw_name = entry.split(b'\t',1)
        _, kind, oid = metadata.split()
        name = raw_name.decode('utf-8')
        if kind == b'blob' and allowed(name): blobs[name] = oid.decode('ascii')
    process = subprocess.run(['git','cat-file','--batch'],cwd=root,
        input=('\n'.join(blobs.values())+'\n').encode('ascii') if blobs else b'',
        stdout=subprocess.PIPE,check=True)
    hashes, offset = {}, 0
    for name,oid in blobs.items():
        end = process.stdout.index(b'\n',offset)
        actual,kind,size = process.stdout[offset:end].split()
        if actual.decode('ascii') != oid or kind != b'blob':
            raise ValueError('Unable to read committed source: '+name)
        offset = end+1
        data = process.stdout[offset:offset+int(size)]
        if len(data) != int(size): raise ValueError('Incomplete committed source: '+name)
        hashes[name] = hashlib.sha256(data).hexdigest()
        offset += int(size)+1
    return commit,hashes


def package_hashes(path):
    """Accept exact, verified bytes from an explicitly selected published ZIP."""
    from windows_offline.apply_update import allowed,safe_path
    data = path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)): raise ValueError('Duplicate files in accepted baseline package')
        manifests = [name for name in ('update.json','readonly-update.json') if name in names]
        if len(manifests) != 1: raise ValueError('Unrecognized accepted baseline package')
        manifest = json.loads(archive.read(manifests[0]))
        if not isinstance(manifest.get('revision'),str) or not manifest['revision']:
            raise ValueError('Accepted baseline package lacks a revision')
        hashes = {}
        for name,info in manifest['files'].items():
            # Check every declared payload, but never extend the source allowlist.
            payload_hash = hashlib.sha256(archive.read('payload/'+name)).hexdigest()
            if payload_hash != info['sha256']:
                raise ValueError('Damaged accepted baseline package source: '+name)
            if allowed(name):
                safe_path(ROOT,name)
                hashes[name] = payload_hash
    evidence = dict(name=path.name,sha256=hashlib.sha256(data).hexdigest(),
        revision=manifest['revision'],files=len(hashes))
    return evidence,hashes


def build(output,accepted_baseline_revisions=(),accepted_baseline_packages=(),history_interval_seconds=None,
          defer_large_snapshot_check=False):
    if history_interval_seconds is not None and (type(history_interval_seconds) is not int or history_interval_seconds not in (30,60)):
        raise ValueError('Only explicit integer collector history intervals of 30 or 60 seconds are supported')
    if type(defer_large_snapshot_check) is not bool:
        raise ValueError('Deferred snapshot verification must be explicitly enabled with a boolean')
    latest = json.loads((output/'latest.json').read_text(encoding='utf-8'))
    baseline = json.loads((Path(latest['stage'])/'manifest.json').read_text(encoding='utf-8'))
    settings = json.loads((output/'transfer-settings.json').read_text(encoding='utf-8'))
    tracked = subprocess.check_output(['git','ls-files'],cwd=ROOT,text=True).splitlines()
    tracked += ['backend/client_mode.py','backend/app/gpt_density/hourly_anchor.py',
                'backend/migrations/versions/c3a610160001_0014_production_assay_time.py']
    from windows_offline.apply_update import allowed,manual_float_output_compatibility,MANUAL_OUTPUT_SCHEMA_PATH
    accepted,commits = {},[]
    def accept_compatible_output(data):
        digest = hashlib.sha256(manual_float_output_compatibility(data)).hexdigest()
        accepted.setdefault(MANUAL_OUTPUT_SCHEMA_PATH,set()).add(digest)
    original_schema = Path(latest['stage'])/'project'/MANUAL_OUTPUT_SCHEMA_PATH
    original_digest = baseline['files'].get('project/'+MANUAL_OUTPUT_SCHEMA_PATH)
    if original_digest and original_schema.is_file():
        data = original_schema.read_bytes()
        if hashlib.sha256(data).hexdigest() != original_digest:
            raise ValueError('Original offline schema source does not match its manifest')
        accept_compatible_output(data)
    for revision in accepted_baseline_revisions:
        commit,hashes = revision_hashes(revision,root=ROOT)
        if commit in commits: continue
        commits.append(commit)
        for name,digest in hashes.items(): accepted.setdefault(name,set()).add(digest)
        if MANUAL_OUTPUT_SCHEMA_PATH in hashes:
            accept_compatible_output(subprocess.check_output(['git','show',commit+':'+MANUAL_OUTPUT_SCHEMA_PATH],cwd=ROOT))
    packages = []
    for package in accepted_baseline_packages:
        evidence,hashes = package_hashes(Path(package))
        if any(item['sha256'] == evidence['sha256'] for item in packages): continue
        packages.append(evidence)
        for name,digest in hashes.items(): accepted.setdefault(name,set()).add(digest)
        if MANUAL_OUTPUT_SCHEMA_PATH in hashes:
            with zipfile.ZipFile(package) as archive:
                accept_compatible_output(archive.read('payload/'+MANUAL_OUTPUT_SCHEMA_PATH))
    files,payload = {},{}
    for name in sorted(set(tracked)):
        if not allowed(name): continue
        path = ROOT/name
        if not path.is_file(): continue
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        previous = baseline['files'].get('project/'+name)
        known = accepted.get(name,set())
        if previous == digest and not known.difference({digest}): continue
        files[name] = dict(sha256=digest,previous_sha256=previous)
        if known: files[name]['accepted_previous_sha256'] = sorted(known)
        payload['payload/'+name] = data
    revision = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    manifest = dict(format=1,revision=revision,created_at=datetime.now().astimezone().isoformat(),
        report_url=f'http://{settings["bind"]}:{settings["port"]}/{settings["token"]}/status',files=files)
    if {'backend/app/schema_upgrade.py',MANUAL_OUTPUT_SCHEMA_PATH}.issubset(files):
        manifest['rollback_compatibility'] = 'manual-float-ash-schema-v1'
    if history_interval_seconds is not None:
        manifest['collector_history_interval_seconds'] = history_interval_seconds
    if defer_large_snapshot_check:
        manifest['snapshot_content_check_policy'] = 'maintenance-window'
    if commits: manifest['accepted_baseline_revisions'] = commits
    if packages: manifest['accepted_baseline_packages'] = packages
    target = output/'extras/ProjectUpdate.zip'
    target.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('update.json',json.dumps(manifest,indent=2).encode('utf-8'))
        for name,data in payload.items(): archive.writestr(name,data)
        for name in ('apply_update.py','UpdateProject.cmd','StopOwnedWeb.ps1','StopOwnedCollector.ps1'):
            archive.write(ROOT/'backend/scripts/windows_offline'/name,name)
    print(json.dumps(dict(package=str(target),bytes=target.stat().st_size,files=len(files),revision=revision)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT/'outputs/offline-win10-x64')
    parser.add_argument('--accepted-baseline-revision',action='append',default=[],
        help='Explicitly accept committed source hashes from this previously published revision; repeatable')
    parser.add_argument('--accepted-baseline-package',type=Path,action='append',default=[],
        help='Explicitly accept verified payload hashes from this previously published update ZIP; repeatable')
    parser.add_argument('--history-interval-seconds',type=int,choices=[30,60],
        help='Explicitly set only the installed collector history interval to 30 or 60 seconds; omitted preserves configuration')
    parser.add_argument('--defer-large-snapshot-check',action='store_true',
        help='Keep the consistent backup and structural check, but defer content scans above 64 MiB to a maintenance window; such backups remain unverified')
    args = parser.parse_args()
    build(args.output.resolve(),args.accepted_baseline_revision,args.accepted_baseline_package,args.history_interval_seconds,
          args.defer_large_snapshot_check)
