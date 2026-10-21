"""Exercise the Windows helper against harmless disposable processes only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest


HELPER = Path(__file__).resolve().parents[1]/'scripts/windows_offline/StopOwnedCollector.ps1'
pytestmark = pytest.mark.skipif(os.name != 'nt' or not shutil.which('powershell.exe'),reason='Windows process ownership helper')


def powershell(*args):
    return subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass',*args],
        capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=40)


def test_helper_stops_only_exact_owned_script_and_preserves_other_python(tmp_path):
    root,other = tmp_path/'installation with spaces',tmp_path/'other installation'
    for base in (root,other):
        (base/'backend').mkdir(parents=True)
        (base/'offline-install.json').write_text('{}')
        (base/'backend/collector.py').write_text('import time\ntime.sleep(90)\n')
    children = []
    try:
        owned = subprocess.Popen([sys.executable,'-u',str(root/'backend/collector.py')],
            creationflags=subprocess.CREATE_NO_WINDOW)
        other_process = subprocess.Popen([sys.executable,'-u',str(other/'backend/collector.py')],
            creationflags=subprocess.CREATE_NO_WINDOW)
        # An exact path passed as a -c program argument must never count as the script.
        argument_only = subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)',str(root/'backend/collector.py')],
            creationflags=subprocess.CREATE_NO_WINDOW)
        children.extend((owned,other_process,argument_only))
        result = powershell('-File',str(HELPER),'-InstallDir',str(root))
        assert result.returncode == 0,result.stdout+result.stderr
        owned.wait(timeout=5)
        assert owned.returncode is not None
        assert other_process.poll() is None and argument_only.poll() is None
        assert 'Owned collector stopped: PID ' in result.stdout
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)


def test_pid_creation_time_is_rechecked_before_stopping(tmp_path):
    root = tmp_path/'host'
    root.mkdir()
    (root/'offline-install.json').write_text('{}')
    script = tmp_path/'test-reused-pid.ps1'
    # Mock only process enumeration and stopping. The production parser and
    # ownership predicate execute unchanged; no real process can be stopped.
    quoted_root = str(root).replace("'","''")
    quoted_helper = str(HELPER).replace("'","''")
    script.write_text("""$ErrorActionPreference = 'Stop'
$script:StopCalls = 0
$script:QueryCount = 0
function Get-CimInstance {
    param($ClassName,$Filter)
    $script:QueryCount++
    $taskCreated = if ($script:QueryCount -eq 1) { [DateTime]'2026-01-01' } else { [DateTime]'2026-01-02' }
    [pscustomobject]@{ Name='python.exe'; ProcessId=2147483000; CreationDate=$taskCreated;
        CommandLine='python.exe -u """+'"'+quoted_root+"""\\backend\\collector.py"' }
}
function Stop-Process { param($Id,$Force,$ErrorAction); $script:StopCalls++ }
. '"""+quoted_helper+"""' -InstallDir '"""+quoted_root+"""'
if ($script:StopCalls -ne 0) { throw 'A reused PID was stopped' }
Write-Output 'reused-pid-preserved'
""",encoding='utf-8')
    result = powershell('-File',str(script))
    assert result.returncode == 0,result.stdout+result.stderr
    assert 'reused-pid-preserved' in result.stdout
