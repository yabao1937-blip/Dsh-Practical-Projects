"""StopOwnedWeb.ps1 must stay scoped to this installation's python web processes.

2026-10-21: an unfiltered Win32_Process sweep paged the whole WMI repository on
a 4 GiB host right after a 6 GiB backup and blew the caller's 120s timeout,
failing an otherwise healthy update. The helper must filter by process name
server-side (like StopOwnedCollector.ps1) and never touch other installations.

Port 8000 on the machine running these tests may belong to any installation, so
process enumeration is mocked (like test_stop_owned_collector.py): only the
production ownership predicate and stop flow execute against synthetic rows.
"""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


HELPER = Path(__file__).resolve().parents[1]/'scripts/windows_offline/StopOwnedWeb.ps1'
pytestmark = pytest.mark.skipif(os.name != 'nt' or not shutil.which('powershell.exe'),
    reason='Windows web ownership helper')


def powershell(*args):
    return subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass',*args],
        capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=40)


def ps_quote(value):
    return str(value).replace("'","''")


def run_harness(tmp_path, installation, rows, epilogue):
    """Neutralize Get-NetTCPConnection and mock Win32_Process enumeration with
    the given synthetic rows; dot-source the real helper inside. The mock
    honors ProcessId filters because the helper rechecks the exact process
    (creation time) before stopping it."""
    harness = tmp_path/'owned-web-harness.ps1'
    body = ["$ErrorActionPreference = 'Stop'",
            "$script:StopCalls = 0",
            "$script:QueryFilters = @()",
            'function Get-NetTCPConnection { param($LocalPort,$State,$ErrorAction) $null }',
            'function Get-CimInstance {',
            '  param($ClassName,$Filter)',
            '  $script:QueryFilters += [string]$Filter',
            '  $taskRows = @(']
    for index,(pid,created,command) in enumerate(rows):
        separator = ',' if index < len(rows)-1 else ''
        body.append("    [pscustomobject]@{ Name='python.exe'; ProcessId=%d; CreationDate=[DateTime]'%s'; CommandLine='%s' }%s"
            % (pid, created, ps_quote(command), separator))
    body += ['  )',
             "  $taskWanted = if ($Filter -match 'ProcessId=(\\d+)') { [int]$Matches[1] } else { $null }",
             '  if ($null -ne $taskWanted) { ,@($taskRows | Where-Object { $_.ProcessId -eq $taskWanted }) } else { $taskRows }',
             '}',
             'function Stop-Process { param($Id,[switch]$Force,$ErrorAction); $script:StopCalls++ }',
             ". '"+ps_quote(str(HELPER))+"' -InstallDir '"+ps_quote(str(installation))+"'",
             epilogue]
    harness.write_text('\n'.join(body), encoding='utf-8-sig')
    return powershell('-File',str(harness))


def make_installation(base):
    (base/'backend').mkdir(parents=True)
    (base/'offline-install.json').write_text('{}')


def test_wmi_query_filters_by_python_process_name_and_stops_owned_web(tmp_path):
    owned = tmp_path/'installation with spaces'
    make_installation(owned)
    command = 'python.exe -u "'+str(owned/'backend/serve.py')+'"'
    result = run_harness(tmp_path, owned,
        [(2147483001,'2026-01-01',command)],
        "Write-Output ('stops='+$script:StopCalls+' filters='+($script:QueryFilters -join '|'))")
    assert result.returncode == 0, result.stdout+result.stderr
    assert 'stops=1' in result.stdout, result.stdout
    assert "Name='python.exe' OR Name='pythonw.exe'" in result.stdout, result.stdout
    assert 'Owned web process stopped' in result.stdout, result.stdout


def test_preserves_other_installations_and_unrelated_python(tmp_path):
    owned, other = tmp_path/'owned', tmp_path/'other installation'
    make_installation(owned); make_installation(other)
    rows = [
        (2147483001,'2026-01-01','python.exe -u "'+str(owned/'backend/serve.py')+'"'),
        (2147483002,'2026-01-01','python.exe -u "'+str(other/'backend/serve.py')+'"'),
        (2147483003,'2026-01-01','python.exe -c "import time; time.sleep(90)"'),
    ]
    result = run_harness(tmp_path, owned, rows,
        "Write-Output ('stops='+$script:StopCalls)")
    assert result.returncode == 0, result.stdout+result.stderr
    assert 'stops=1' in result.stdout, result.stdout
    assert 'Owned web process stopped' in result.stdout, result.stdout


def test_reused_pid_is_not_stopped(tmp_path):
    owned = tmp_path/'host'
    make_installation(owned)
    command = 'python.exe -u "'+str(owned/'backend/serve.py')+'"'
    body = ["$ErrorActionPreference = 'Stop'",
            '$script:StopCalls = 0',
            '$script:QueryCount = 0',
            'function Get-NetTCPConnection { param($LocalPort,$State,$ErrorAction) $null }',
            'function Get-CimInstance {',
            '  param($ClassName,$Filter)',
            '  $script:QueryCount++',
            "  $taskCreated = if ($script:QueryCount -eq 1) { [DateTime]'2026-01-01' } else { [DateTime]'2026-01-02' }",
            "  ,@([pscustomobject]@{ Name='python.exe'; ProcessId=2147483000; CreationDate=$taskCreated;",
            "      CommandLine='"+ps_quote(command)+"' })",
            '}',
            'function Stop-Process { param($Id,$Force,$ErrorAction); $script:StopCalls++ }',
            ". '"+ps_quote(str(HELPER))+"' -InstallDir '"+ps_quote(str(owned))+"'",
            "if ($script:StopCalls -ne 0) { throw 'A reused PID was stopped' }",
            "Write-Output 'reused-pid-preserved'"]
    harness = tmp_path/'reused-pid.ps1'
    harness.write_text('\n'.join(body), encoding='utf-8-sig')
    result = powershell('-File',str(harness))
    assert result.returncode == 0, result.stdout+result.stderr
    assert 'reused-pid-preserved' in result.stdout


def test_rejects_unrecognized_installation(tmp_path):
    root = tmp_path/'not-an-installation'
    root.mkdir()
    result = powershell('-File',str(HELPER),'-InstallDir',str(root))
    assert result.returncode != 0
    assert 'Not a recognized installation' in (result.stdout+result.stderr)
