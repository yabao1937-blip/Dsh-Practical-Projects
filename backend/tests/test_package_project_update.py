"""Only explicitly selected published commits broaden the updater baseline."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest


SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
spec = importlib.util.spec_from_file_location('project_update_builder',SCRIPTS/'package_project_update.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.fixture
def repository(tmp_path,monkeypatch):
    root,output = tmp_path/'repo',tmp_path/'output'
    root.mkdir()
    def git(*args):
        return subprocess.check_output(['git',*args],cwd=root,stderr=subprocess.DEVNULL,text=True).strip()
    git('init','-q')
    git('config','user.name','Update fixture')
    git('config','user.email','fixture@example.invalid')
    git('config','core.autocrlf','false')
    source = root/'frontend/app.js'
    source.parent.mkdir()
    revisions = []
    for value in ('original','published','current'):
        source.write_text(value)
        git('add','frontend/app.js')
        git('commit','-qm',value)
        revisions.append(git('rev-parse','HEAD'))
    helper_root = root/'backend/scripts/windows_offline'
    helper_root.mkdir(parents=True)
    for name in ('apply_update.py','UpdateProject.cmd','StopOwnedWeb.ps1','StopOwnedCollector.ps1'):
        shutil.copyfile(SCRIPTS/'windows_offline'/name,helper_root/name)
    output.mkdir()
    stage = tmp_path/'stage'
    stage.mkdir()
    (output/'latest.json').write_text(json.dumps(dict(stage=str(stage))))
    (stage/'manifest.json').write_text(json.dumps(dict(files={'project/frontend/app.js':hashlib.sha256(b'original').hexdigest()})))
    (output/'transfer-settings.json').write_text(json.dumps(dict(bind='127.0.0.1',port=8765,token='do-not-print-this-token')))
    monkeypatch.setattr(builder,'ROOT',root)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return root,output,revisions,git


def test_default_has_no_additional_history_and_bundles_collector_helper(repository,capsys):
    _,output,revisions,_ = repository
    builder.build(output)
    with zipfile.ZipFile(output/'extras/ProjectUpdate.zip') as archive:
        manifest = json.loads(archive.read('update.json'))
        assert manifest['revision'] == revisions[-1]
        assert 'accepted_baseline_revisions' not in manifest
        assert 'accepted_previous_sha256' not in manifest['files']['frontend/app.js']
        assert 'StopOwnedCollector.ps1' in archive.namelist()
    assert 'do-not-print-this-token' not in capsys.readouterr().out


def test_explicit_history_hashes_are_committed_bytes_and_repeatable(repository,capsys):
    root,output,revisions,_ = repository
    # An unpublished file at a historical path must never become a known baseline.
    (root/'frontend/app.js').write_text('unpublished payload')
    builder.build(output,[revisions[0],revisions[1],revisions[1]])
    with zipfile.ZipFile(output/'extras/ProjectUpdate.zip') as archive:
        manifest = json.loads(archive.read('update.json'))
        info = manifest['files']['frontend/app.js']
        assert manifest['accepted_baseline_revisions'] == revisions[:2]
        assert set(info['accepted_previous_sha256']) == {hashlib.sha256(value).hexdigest() for value in (b'original',b'published')}
        assert hashlib.sha256(b'unpublished payload').hexdigest() not in info['accepted_previous_sha256']
    assert 'do-not-print-this-token' not in capsys.readouterr().out


def test_reverted_original_file_is_packaged_when_selected_history_differs(repository):
    root,output,revisions,git = repository
    (root/'frontend/app.js').write_text('original')
    git('add','frontend/app.js')
    git('commit','-qm','revert published file')
    builder.build(output,[revisions[1]])
    with zipfile.ZipFile(output/'extras/ProjectUpdate.zip') as archive:
        assert archive.read('payload/frontend/app.js') == b'original'


def test_unknown_revision_fails_before_writing_package(repository):
    _,output,_,_ = repository
    with pytest.raises(subprocess.CalledProcessError): builder.build(output,['not-a-published-commit'])
    assert not (output/'extras/ProjectUpdate.zip').exists()


def baseline_zip(path,revision,*,damage=False):
    # Exact mixed line endings are deliberately distinct from committed Git bytes.
    source = b'previously\r\npublished\nbytes\r\n'
    model = b'never a source baseline'
    files = {'frontend/app.js':dict(sha256=hashlib.sha256(source).hexdigest()),
        'backend/models/protected.json':dict(sha256=hashlib.sha256(model).hexdigest())}
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('update.json',json.dumps(dict(revision=revision,files=files)))
        archive.writestr('payload/frontend/app.js',b'damaged' if damage else source)
        archive.writestr('payload/backend/models/protected.json',model)
    return source


def test_explicit_package_adds_only_verified_exact_allowed_source_hashes(repository,capsys):
    _,output,revisions,_ = repository
    package = output/'published.zip'
    source = baseline_zip(package,revisions[1])
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    builder.build(output,accepted_baseline_packages=[package,package])
    with zipfile.ZipFile(output/'extras/ProjectUpdate.zip') as archive:
        manifest = json.loads(archive.read('update.json'))
        assert manifest['files']['frontend/app.js']['accepted_previous_sha256'] == [hashlib.sha256(source).hexdigest()]
        assert manifest['accepted_baseline_packages'] == [dict(name='published.zip',sha256=digest,revision=revisions[1],files=1)]
        assert 'backend/models/protected.json' not in manifest['files']
    assert 'do-not-print-this-token' not in capsys.readouterr().out


def test_damaged_baseline_package_never_broadens_accepted_hashes(repository):
    _,output,revisions,_ = repository
    package = output/'damaged.zip'
    baseline_zip(package,revisions[1],damage=True)
    with pytest.raises(ValueError,match='Damaged accepted baseline'):
        builder.build(output,accepted_baseline_packages=[package])
    assert not (output/'extras/ProjectUpdate.zip').exists()
