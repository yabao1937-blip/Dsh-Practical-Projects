"""Real temporary Windows processes; never stop a production or development Web."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/host_runs_switch/StopOwnedWeb.ps1'


@pytest.mark.skipif(os.name != 'nt', reason='Windows process identity helper')
@pytest.mark.parametrize('relative,foreign', [(True,False), (True,True), (False,False), (False,True)])
def test_real_web_script_identity_uses_exact_path_and_cwd(tmp_path, relative, foreign):
    root, other = tmp_path/'owned', tmp_path/'foreign'
    for directory in (root, other):
        (directory/'backend').mkdir(parents=True)
        (directory/'offline-install.json').write_text('{}')
        (directory/'backend/serve.py').write_text('import time\ntime.sleep(90)\n')
    directory = other if foreign else root
    process = subprocess.Popen([sys.executable, '-u', 'serve.py' if relative else str(directory/'backend/serve.py')],
        cwd=directory/'backend', stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        # Load definitions only; neither Get-NetTCPConnection nor Kill is run.
        definitions = SCRIPT.read_text(encoding='utf-8').split('$taskListeners = @(', 1)[0]
        probe = tmp_path/'identity.ps1'
        probe.write_text(definitions + '\n'
            '$taskCandidate = Get-CimInstance Win32_Process -Filter ("ProcessId=" + ' + str(process.pid) + ')\n'
            '$taskProcess = [Diagnostics.Process]::GetProcessById(' + str(process.pid) + ')\n'
            'try { $null = $taskProcess.Handle; $taskActual = Get-WebScript $taskCandidate $taskProcess; '
            '@{owned=($taskActual -ieq $taskWeb)} | ConvertTo-Json -Compress } '
            'finally { $taskProcess.Dispose() }\n', encoding='utf-8-sig')
        result = subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(probe),
            '-InstallDir',str(root)], capture_output=True, text=True, timeout=45)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['owned'] is (not foreign)
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.mark.skipif(os.name != 'nt', reason='Windows process identity helper')
def test_every_foreign_8000_listener_blocks_before_killing_anything(tmp_path):
    root = tmp_path/'owned'
    (root/'backend').mkdir(parents=True)
    (root/'offline-install.json').write_text('{}')
    (root/'backend/serve.py').write_text('import time\ntime.sleep(90)\n')
    # Only the listeners are synthetic. The first belongs to a real owned
    # serve.py; the second is foreign and must prevent even the first kill.
    probe = tmp_path/'foreign-listener.ps1'
    process = subprocess.Popen([sys.executable, '-u', 'serve.py'], cwd=root/'backend',
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        probe.write_text('param([string]$InstallDir)\n'
            'function Get-NetTCPConnection { @([pscustomobject]@{OwningProcess=' + str(process.pid) + '}, '
            '[pscustomobject]@{OwningProcess=' + str(os.getpid()) + '}) }\n'
            '& ' + "'" + str(SCRIPT).replace("'", "''") + "' -InstallDir $InstallDir\n", encoding='utf-8-sig')
        result = subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(probe),
            '-InstallDir',str(root)], capture_output=True, text=True, timeout=45)
        assert result.returncode != 0
        assert 'another or unproven' in result.stderr
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=10)
