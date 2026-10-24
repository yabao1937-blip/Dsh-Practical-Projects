"""A minute query fix cannot turn the service module into a broad write hotfix."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import zipfile

import pytest

from test_project_update import update

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
ORIGINAL = b'''import math
CADENCE = 60

def periodic_batch_ready(db, now, sources=None, *, cadence_seconds=CADENCE):
    """One read-only readiness query."""
    return db.read(now, sources)

def save_record(db, row):
    db.add(row)
'''
UPDATED = ORIGINAL.replace(b'return db.read(now, sources)',
                           b'return db.read_range(now, sources, cadence_seconds)')


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def patch(tmp_path,monkeypatch):
    package,root = tmp_path/'package',tmp_path/'installation'
    payload = package/'payload'/update.PERIODIC_QUERY_PATH
    host = root/update.PERIODIC_QUERY_PATH
    for file in (payload,host):
        file.parent.mkdir(parents=True,exist_ok=True)
    payload.write_bytes(UPDATED)
    host.write_bytes(ORIGINAL)
    proof = package/update.PERIODIC_QUERY_PROOF
    proof.parent.mkdir()
    proof.write_bytes(ORIGINAL)
    monkeypatch.setattr(update,'PERIODIC_QUERY_BASELINE',sha(ORIGINAL))
    (root/'offline-install.json').write_text('{}')
    (root/'Start.ps1').write_text('fixture')
    database = root/'backend/data/dense_medium.db'
    database.parent.mkdir()
    connection = sqlite3.connect(database)
    try:
        connection.execute('CREATE TABLE evidence(value)')
        connection.execute('INSERT INTO evidence VALUES(1)')
        connection.commit()
    finally:
        connection.close()
    manifest = dict(format=1,profile='query-only-hotfix',function_patch=update.PERIODIC_QUERY_PATCH,
        revision='reviewed',files={update.PERIODIC_QUERY_PATH:dict(sha256=sha(UPDATED),previous_sha256=sha(ORIGINAL))})
    calls = []
    monkeypatch.setattr(update,'powershell',lambda script,*args,**kwargs:calls.append(Path(script).name))
    monkeypatch.setattr(update,'restart',lambda root:calls.append('restart'))
    monkeypatch.setattr(update,'backup_database',lambda *a,**kw:pytest.fail('A read-only function hotfix must not copy the database'))
    monkeypatch.setattr(update,'collector_status',lambda *a,**kw:pytest.fail('Collector must remain untouched'))
    monkeypatch.setattr(update,'collector_ready',lambda *a,**kw:pytest.fail('Collector must remain untouched'))
    monkeypatch.setattr(update,'urlopen',lambda url,**kwargs:io.BytesIO(
        json.dumps(dict(advice_policy='gpt-hourly-reference-v4')).encode() if 'hourly' in url else '实时总精煤灰分'.encode()))
    return package,root,payload,host,proof,manifest,calls


def test_real_deployment_baseline_is_pinned_to_the_reviewed_release():
    assert update.PERIODIC_QUERY_BASELINE == 'cbef07c65508d228a1c82487ddb8cd347768ee8c08188d730caf31d746bb6edd'


def test_function_hotfix_keeps_collector_and_database_and_is_idempotent(patch):
    package,root,payload,host,proof,manifest,calls = patch
    before = (root/'backend/data/dense_medium.db').read_bytes()
    backup = update.apply(package,root,manifest)
    assert host.read_bytes() == UPDATED
    assert (backup/'source'/update.PERIODIC_QUERY_PATH).read_bytes() == ORIGINAL
    assert not (backup/'dense_medium.db').exists()
    record = json.loads((root/'source-update.json').read_text())
    assert record['database_snapshot'] is False and record['collector_restarted'] is False
    assert calls == ['StopOwnedWeb.ps1','restart']
    assert (root/'backend/data/dense_medium.db').read_bytes() == before
    calls.clear()
    update.apply(package,root,manifest)
    assert calls == ['StopOwnedWeb.ps1','restart']


def test_hotfix_failed_web_restart_rolls_source_back_without_losing_new_data(patch,monkeypatch):
    package,root,_,host,_,manifest,calls = patch
    count = 0
    def restart(root):
        nonlocal count
        count += 1
        if count == 1:
            database = sqlite3.connect(root/'backend/data/dense_medium.db')
            try:
                database.execute('INSERT INTO evidence VALUES(2)')
                database.commit()
            finally:
                database.close()
            raise RuntimeError('web-start-failed')
    monkeypatch.setattr(update,'restart',restart)
    with pytest.raises(RuntimeError,match='web-start-failed'):
        update.apply(package,root,manifest)
    assert host.read_bytes() == ORIGINAL and count == 2
    assert calls == ['StopOwnedWeb.ps1','StopOwnedWeb.ps1']
    with sqlite3.connect(root/'backend/data/dense_medium.db') as database:
        assert database.execute('SELECT * FROM evidence').fetchall() == [(1,),(2,)]


@pytest.mark.parametrize('change',[
    lambda data:data.replace(b'db.add(row)',b'db.delete(row)'),
    lambda data:data.replace(b'import math',b'import os'),
    lambda data:data.replace(b'CADENCE = 60',b'CADENCE = 5'),
    lambda data:data.replace(b'now, sources=None',b'now, sources=None, extra=None'),
    lambda data:data.replace(b'def periodic_batch_ready',b'@staticmethod\ndef periodic_batch_ready'),
    lambda data:data.replace(b'def periodic_batch_ready',b'async def periodic_batch_ready'),
    lambda data:data+b'\ndef periodic_batch_ready(db):\n    return True\n',
    lambda data:data.replace(b'def periodic_batch_ready',b'def another_function'),
    lambda data:data.replace(b'cadence_seconds=CADENCE',b'cadence_seconds=30'),
])
def test_changed_protected_code_signature_or_duplicate_target_is_rejected(patch,change):
    package,root,payload,host,_,manifest,calls = patch
    changed = change(UPDATED)
    payload.write_bytes(changed)
    manifest['files'][update.PERIODIC_QUERY_PATH]['sha256'] = sha(changed)
    with pytest.raises(ValueError,match='Periodic query patch'):
        update.apply(package,root,manifest)
    assert host.read_bytes() == ORIGINAL and not calls
    assert not (root/'backups').exists()


@pytest.mark.parametrize('cause',['host','proof','declared','accepted_hash','second_file','physical_extra',
                                  'history','snapshot','profile','marker','unchanged'])
def test_baseline_payload_and_policy_scope_fail_before_any_mutation(patch,cause):
    package,root,payload,host,proof,manifest,calls = patch
    info = manifest['files'][update.PERIODIC_QUERY_PATH]
    if cause == 'host': host.write_bytes(ORIGINAL+b'# unknown host edit')
    elif cause == 'proof': proof.write_bytes(ORIGINAL+b'# unknown proof')
    elif cause == 'declared': info['previous_sha256'] = '0'*64
    elif cause == 'accepted_hash': info['accepted_previous_sha256'] = [sha(ORIGINAL)]
    elif cause == 'second_file': manifest['files']['backend/app/telemetry/worker.py'] = dict(sha256='0'*64)
    elif cause == 'physical_extra': (payload.parent/'extra.py').write_text('unexpected')
    elif cause == 'history': manifest['collector_history_interval_seconds'] = 60
    elif cause == 'snapshot': manifest['snapshot_content_check_policy'] = 'maintenance-window'
    elif cause == 'profile': manifest.pop('profile')
    elif cause == 'marker': manifest['function_patch'] = 'unknown'
    elif cause == 'unchanged': payload.write_bytes(ORIGINAL); info['sha256'] = sha(ORIGINAL)
    before = host.read_bytes()
    with pytest.raises(ValueError):
        update.apply(package,root,manifest)
    assert host.read_bytes() == before and not calls
    assert not (root/'backups').exists()


def test_unmarked_service_module_remains_outside_generic_query_allowlist(patch):
    package,root,_,host,_,manifest,calls = patch
    manifest.pop('function_patch')
    with pytest.raises(ValueError,match='cannot change schema'):
        update.apply(package,root,manifest)
    assert host.read_bytes() == ORIGINAL and not calls


@pytest.fixture
def builder_repository(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location('periodic_query_builder',SCRIPTS/'package_periodic_query_hotfix.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    root,output = tmp_path/'repo',tmp_path/'output'
    root.mkdir()
    def git(*args):
        return subprocess.check_output(['git',*args],cwd=root,stderr=subprocess.DEVNULL,text=True).strip()
    git('init','-q');git('config','user.name','Hotfix fixture');git('config','user.email','fixture@example.invalid')
    git('config','core.autocrlf','false')
    target = root/update.PERIODIC_QUERY_PATH
    target.parent.mkdir(parents=True)
    target.write_bytes(ORIGINAL)
    helpers = root/'backend/scripts/windows_offline'
    helpers.mkdir(parents=True)
    for name in ('apply_update.py','UpdateProject.cmd','StopOwnedWeb.ps1'):
        shutil.copyfile(SCRIPTS/'windows_offline'/name,helpers/name)
    git('add','.');git('commit','-qm','accepted baseline')
    base = git('rev-parse','HEAD')
    output.mkdir()
    (output/'transfer-settings.json').write_text(json.dumps(dict(bind='127.0.0.1',port=8765,token='do-not-print')))
    installed = output/'accepted-minute.zip'
    with zipfile.ZipFile(installed,'w') as archive:
        archive.writestr('update.json',json.dumps(dict(revision=base,files={
            update.PERIODIC_QUERY_PATH:dict(sha256=sha(ORIGINAL))})))
        archive.writestr('payload/'+update.PERIODIC_QUERY_PATH,ORIGINAL)
    target.write_bytes(UPDATED)
    git('add','.');git('commit','-qm','reviewed function query fix')
    monkeypatch.setattr(builder,'ROOT',root)
    monkeypatch.setattr(builder,'PERIODIC_QUERY_BASELINE',sha(ORIGINAL))
    monkeypatch.setattr(builder,'INSTALLED_PACKAGE_SHA256',sha(installed.read_bytes()))
    return builder,root,output,installed,base,git


def test_special_packager_ships_one_committed_payload_proof_and_web_helpers(builder_repository,capsys):
    builder,root,output,installed,base,git = builder_repository
    # An uncommitted change is never packaged as the reviewed function fix.
    (root/update.PERIODIC_QUERY_PATH).write_bytes(UPDATED+b'# uncommitted')
    builder.build(base,output,installed)
    with zipfile.ZipFile(output/'extras/ProjectUpdate.zip') as archive:
        manifest = json.loads(archive.read('update.json'))
        assert manifest['profile'] == 'query-only-hotfix'
        assert manifest['function_patch'] == update.PERIODIC_QUERY_PATCH
        assert set(manifest['files']) == {update.PERIODIC_QUERY_PATH}
        assert archive.read('payload/'+update.PERIODIC_QUERY_PATH) == UPDATED
        assert archive.read(update.PERIODIC_QUERY_PROOF) == ORIGINAL
        command = archive.read('UpdateProject.cmd')
        assert b'\r\r\n' not in command and b'\n' not in command.replace(b'\r\n',b'')
        assert 'StopOwnedCollector.ps1' not in archive.namelist()
        assert 'collector_history_interval_seconds' not in manifest
        assert 'snapshot_content_check_policy' not in manifest
        assert manifest['revision'] == git('rev-parse','HEAD')
    assert 'do-not-print' not in capsys.readouterr().out


@pytest.mark.parametrize('cause',['zip','revision','updater'])
def test_special_packager_rejects_unknown_base_or_uncommitted_guard(builder_repository,cause):
    builder,root,output,installed,base,git = builder_repository
    if cause == 'zip': installed.write_bytes(b'unknown package')
    elif cause == 'revision': base = git('rev-parse','HEAD')
    else:
        with (root/'backend/scripts/windows_offline/apply_update.py').open('ab') as stream:
            stream.write(b'\n# uncommitted guard change\n')
    with pytest.raises(ValueError): builder.build(base,output,installed)
    assert not (output/'extras/ProjectUpdate.zip').exists()
