"""Source-only deployment validates old bytes and never restores a stale DB."""
import importlib.util
import io
import json
from pathlib import Path
import sqlite3

import pytest

BACKEND = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('project_update',BACKEND/'scripts/windows_offline/apply_update.py')
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


@pytest.fixture
def prepared(tmp_path,monkeypatch):
    package,root = tmp_path/'package',tmp_path/'host'
    source = package/'payload/frontend/js/example.js'
    source.parent.mkdir(parents=True)
    source.write_text('new code')
    target = root/'frontend/js/example.js'
    target.parent.mkdir(parents=True)
    target.write_text('old code')
    (root/'offline-install.json').write_text('{}')
    (root/'Start.ps1').write_text('unused')
    (root/'backend/data').mkdir(parents=True)
    with sqlite3.connect(root/'backend/data/dense_medium.db') as db:
        db.execute('create table evidence (value integer)')
        db.execute('insert into evidence values(1)')
    manifest = dict(revision='test',files={'frontend/js/example.js':dict(
        sha256=update.digest(source),previous_sha256=update.digest(target))})
    calls = []
    monkeypatch.setattr(update,'powershell',lambda *args,**kwargs:calls.append(args))
    monkeypatch.setattr(update,'restart',lambda root:None)
    monkeypatch.setattr(update,'urlopen',lambda url,**kwargs:io.BytesIO(
        json.dumps(dict(advice_policy='gpt-hourly-reference-v4')).encode() if 'hourly' in url else '实时总精煤灰分'.encode()))
    return package,root,source,target,manifest,calls


@pytest.mark.parametrize('name',['../frontend/a','frontend/../../a','C:/data','backend/data/dense_medium.db',
    'backend/config/kepserver.local.json','backend/models/model.json','backend/.venv/python.exe'])
def test_update_cannot_replace_production_data_or_escape_root(tmp_path,name):
    with pytest.raises(ValueError): update.safe_path(tmp_path,name)


def test_modified_host_or_damaged_package_stops_before_mutation(prepared):
    package,root,source,target,manifest,calls = prepared
    target.write_text('host custom code')
    with pytest.raises(ValueError,match='differs'): update.apply(package,root,manifest)
    assert target.read_text() == 'host custom code' and not calls
    source.write_text('damaged payload')
    with pytest.raises(ValueError,match='damaged'): update.apply(package,root,manifest)
    assert not calls and not (root/'backups').exists()


def test_update_keeps_database_and_is_repeatable(prepared):
    package,root,source,target,manifest,calls = prepared
    backup = update.apply(package,root,manifest)
    assert target.read_text() == 'new code'
    assert (backup/'source/frontend/js/example.js').read_text() == 'old code'
    with sqlite3.connect(backup/'dense_medium.db') as db:
        assert db.execute('select * from evidence').fetchall() == [(1,)]
    update.apply(package,root,manifest)
    with sqlite3.connect(root/'backend/data/dense_medium.db') as db:
        assert db.execute('select * from evidence').fetchall() == [(1,)]


def test_failed_start_rolls_back_code_but_keeps_new_production_rows(prepared,monkeypatch):
    package,root,source,target,manifest,calls = prepared
    count = 0
    def fail_once(root):
        nonlocal count
        count += 1
        if count == 1:
            with sqlite3.connect(root/'backend/data/dense_medium.db') as db:
                db.execute('insert into evidence values(2)')
            raise RuntimeError('failed startup')
    monkeypatch.setattr(update,'restart',fail_once)
    with pytest.raises(RuntimeError,match='failed startup'): update.apply(package,root,manifest)
    assert target.read_text() == 'old code' and count == 2
    with sqlite3.connect(root/'backend/data/dense_medium.db') as db:
        assert db.execute('select * from evidence').fetchall() == [(1,),(2,)]


def test_live_wal_backup_is_consistent_and_releases_snapshot_before_check(prepared,tmp_path):
    _,root,_,_,_,_ = prepared
    backup = tmp_path/'snapshot'
    backup.mkdir()
    events = []
    path = root/'backend/data/dense_medium.db'
    with sqlite3.connect(path) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        # More than one batch: write while the copy still has pages remaining.
        writer.execute('CREATE TABLE payload (data blob)')
        writer.executemany('INSERT INTO payload VALUES(zeroblob(1048576))',[()]*10)
        writer.commit()
        inserted = False
        def report(stage,message):
            nonlocal inserted
            events.append((stage,message))
            if 'pages)' in message and not inserted:
                writer.execute('INSERT INTO evidence VALUES(2)')
                writer.commit()
                inserted = True
            if 'checking backup integrity' in message:
                # A leaked source snapshot would prevent truncating the live WAL.
                assert writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
        update.backup_database(root,backup,report)
        assert inserted
        assert writer.execute('SELECT * FROM evidence').fetchall() == [(1,),(2,)]
    with sqlite3.connect(backup/'dense_medium.db') as snapshot:
        assert snapshot.execute('SELECT * FROM evidence').fetchall() == [(1,)]
        assert snapshot.execute('SELECT COUNT(*) FROM payload').fetchone() == (10,)
    assert all(stage == 'copying' for stage,_ in events)
    assert any('checking backup integrity' in message for _,message in events)
    assert 'verified' in events[-1][1]


def test_backup_timeout_leaves_source_and_web_untouched(prepared,monkeypatch):
    package,root,_,target,manifest,calls = prepared
    backup_database = update.backup_database
    monkeypatch.setattr(update,'backup_database',lambda root,backup,report:
        backup_database(root,backup,report,timeout=0))
    with pytest.raises(TimeoutError,match='backup timed out'):
        update.apply(package,root,manifest)
    assert target.read_text() == 'old code'
    assert not calls
    with sqlite3.connect(root/'backend/data/dense_medium.db') as db:
        assert db.execute('SELECT * FROM evidence').fetchall() == [(1,)]


def test_query_hotfix_cannot_skip_snapshot_for_storage_changes(prepared):
    package,root,_,target,manifest,calls = prepared
    manifest['profile'] = 'query-only-hotfix'
    with pytest.raises(ValueError,match='cannot change schema'):
        update.apply(package,root,manifest)
    assert not calls and target.read_text() == 'old code'
    assert not (root/'backups').exists()


def test_allowlisted_query_hotfix_preserves_database_without_full_scan(prepared,monkeypatch):
    package,root,source,target,manifest,calls = prepared
    name = 'frontend/js/api.js'
    source.rename(source.with_name('api.js'))
    target.rename(target.with_name('api.js'))
    manifest['files'] = {name:next(iter(manifest['files'].values()))}
    manifest['profile'] = 'query-only-hotfix'
    monkeypatch.setattr(update,'backup_database',lambda *a,**kw:pytest.fail('Query hotfix must not scan live data'))
    backup = update.apply(package,root,manifest)
    assert (root/name).read_text() == 'new code'
    assert (backup/'source'/name).read_text() == 'old code'
    assert not (backup/'dense_medium.db').exists()
    with sqlite3.connect(root/'backend/data/dense_medium.db') as db:
        assert db.execute('SELECT * FROM evidence').fetchall() == [(1,)]
