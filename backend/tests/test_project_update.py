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
