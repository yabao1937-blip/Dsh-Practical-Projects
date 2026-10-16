"""A local client marker prevents normal entry points from creating a second DB."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

BACKEND = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dmcs_client_mode',BACKEND/'client_mode.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_missing_marker_keeps_host_installation_unchanged(tmp_path):
    assert module.client_url(tmp_path/'missing.json') is None


@pytest.mark.parametrize('url',['file:///C:/data','http://user:pass@host','http://host/path','http://host/?key=abc'])
def test_invalid_marker_fails_closed(tmp_path,url):
    marker = tmp_path/'client.json'
    marker.write_text(json.dumps(dict(mode='client',server_url=url)))
    with pytest.raises(ValueError): module.client_url(marker)


@pytest.mark.parametrize('entry',['serve.py','collector.py','scripts/start_collector.py'])
def test_python_entry_stops_before_database_or_collection(tmp_path,entry):
    root = tmp_path/'backend'
    (root/'data').mkdir(parents=True)
    (root/'scripts').mkdir()
    shutil.copy2(BACKEND/'client_mode.py',root/'client_mode.py')
    shutil.copy2(BACKEND/entry,root/entry)
    (root/'data/client-mode.json').write_text(json.dumps(dict(mode='client',server_url='http://10.255.2.7:8000/')))
    r = subprocess.run([sys.executable,str(root/entry)],cwd=root,capture_output=True,text=True,encoding='utf-8')
    assert r.returncode == 0 and '10.255.2.7:8000' in r.stdout
    assert list((root/'data').iterdir()) == [root/'data/client-mode.json']


def test_server_launcher_honors_marker_without_python_installation(tmp_path):
    shell = shutil.which('pwsh') or shutil.which('powershell')
    if not shell: pytest.skip('PowerShell not installed')
    scripts = tmp_path/'backend/scripts'
    scripts.mkdir(parents=True)
    data = tmp_path/'backend/data'
    data.mkdir()
    shutil.copy2(BACKEND/'scripts/start_server.ps1',scripts/'start_server.ps1')
    (data/'client-mode.json').write_text(json.dumps(dict(mode='client',server_url='http://10.255.2.7:8000/')))
    r = subprocess.run([shell,'-NoProfile','-File',str(scripts/'start_server.ps1'),'-NoBrowser'],
                       capture_output=True,text=True,encoding='utf-8')
    assert r.returncode == 0 and 'Client mode:' in r.stdout
    assert not list(data.glob('*.db'))
