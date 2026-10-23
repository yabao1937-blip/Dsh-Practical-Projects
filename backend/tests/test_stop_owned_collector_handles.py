"""Real Windows process exit checks, including a retained terminated process."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest


HELPER = Path(__file__).resolve().parents[1] / 'scripts/windows_offline/StopOwnedCollector.ps1'
pytestmark = pytest.mark.skipif(
    os.name != 'nt' or not shutil.which('powershell.exe'), reason='Windows process handles')


def powershell(*args, timeout=30):
    return subprocess.run(
        ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', *args],
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)


def query_processes(root):
    result = powershell('-Command',
        "$ErrorActionPreference='Stop'; @((Get-CimInstance Win32_Process "
        "-Filter \"Name='python.exe' OR Name='pythonw.exe'\") | "
        "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine) | ConvertTo-Json -Compress")
    assert result.returncode == 0, result.stdout + result.stderr
    return [row for row in json.loads(result.stdout)
            if row.get('CommandLine') and str(root) in row['CommandLine']]


def wait_for_file(path):
    deadline = time.monotonic() + 10
    while not path.exists():
        assert time.monotonic() < deadline, f'Process did not become ready: {path}'
        time.sleep(.05)


def kernel32():
    library = ctypes.WinDLL('kernel32', use_last_error=True)
    library.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    library.OpenProcess.restype = wintypes.HANDLE
    library.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    library.WaitForSingleObject.restype = wintypes.DWORD
    library.CloseHandle.argtypes = [wintypes.HANDLE]
    library.CloseHandle.restype = wintypes.BOOL
    library.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    library.TerminateProcess.restype = wintypes.BOOL
    return library


def install(root, code):
    (root / 'backend').mkdir(parents=True)
    (root / 'offline-install.json').write_text('{}')
    script = root / 'backend/collector.py'
    script.write_text(code, encoding='utf-8')
    return script


def test_stops_launcher_and_child_with_held_handles_and_preserves_other_installation(tmp_path):
    root = tmp_path / 'owned installation with spaces'
    script = install(root, '''import json, subprocess, sys, time
from pathlib import Path
if len(sys.argv) == 1:
    child = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()), 'child'])
    Path(__file__).with_suffix('.ready').write_text(json.dumps({'child': child.pid}))
time.sleep(90)
''')
    other_script = install(tmp_path / 'another installation', 'import time; time.sleep(90)')
    children, handles = [], []
    api = kernel32()
    try:
        owned = subprocess.Popen([sys.executable, '-u', str(script)], creationflags=subprocess.CREATE_NO_WINDOW)
        unrelated = subprocess.Popen([sys.executable, str(other_script)], creationflags=subprocess.CREATE_NO_WINDOW)
        argument_only = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(90)', str(script)],
            creationflags=subprocess.CREATE_NO_WINDOW)
        children.extend((owned, unrelated, argument_only))
        wait_for_file(script.with_suffix('.ready'))
        # Native handles deliberately outlive execution to model retained
        # launcher/updater handles. Stale CIM visibility is simulated separately.
        candidates = [row for row in query_processes(root) if ' -c ' not in row['CommandLine']]
        assert len(candidates) >= 2
        if sys.prefix != sys.base_prefix:
            launchers = {row['ProcessId'] for row in candidates
                         if os.path.normcase(row.get('ExecutablePath') or '') == os.path.normcase(sys.executable)}
            assert launchers, 'The venv launcher was not captured'
            assert any(row['ParentProcessId'] in launchers
                       and os.path.normcase(row.get('ExecutablePath') or '') == os.path.normcase(sys._base_executable)
                       for row in candidates), 'The real venv interpreter child was not captured'
        for row in candidates:
            handle = api.OpenProcess(0x00100000 | 0x001000 | 0x0001, False, row['ProcessId'])
            assert handle, ctypes.get_last_error()
            handles.append(handle)
        result = powershell('-File', str(HELPER), '-InstallDir', str(root), timeout=25)
        assert result.returncode == 0, result.stdout + result.stderr
        assert all(api.WaitForSingleObject(handle, 0) == 0 for handle in handles)
        assert unrelated.poll() is None and argument_only.poll() is None
    finally:
        for handle in handles:
            if api.WaitForSingleObject(handle, 0) != 0:
                api.TerminateProcess(handle, 1)
            api.CloseHandle(handle)
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)


def test_already_exited_process_with_held_handle_does_not_delay_update(tmp_path):
    root = tmp_path / 'already stopped installation'
    script = install(root, 'import time; time.sleep(90)')
    process = subprocess.Popen([sys.executable, str(script)], creationflags=subprocess.CREATE_NO_WINDOW)
    api, handles = kernel32(), []
    try:
        candidates = query_processes(root)
        assert candidates
        for row in candidates:
            handle = api.OpenProcess(0x00100000 | 0x001000 | 0x0001, False, row['ProcessId'])
            assert handle, ctypes.get_last_error()
            handles.append(handle)
        for handle in handles:
            api.TerminateProcess(handle, 1)
            assert api.WaitForSingleObject(handle, 5000) == 0
        result = powershell('-File', str(HELPER), '-InstallDir', str(root), timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        for handle in handles:
            if api.WaitForSingleObject(handle, 0) != 0:
                api.TerminateProcess(handle, 1)
            api.CloseHandle(handle)
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


def test_stale_cim_identity_does_not_outlive_actual_native_exit(tmp_path):
    root = tmp_path / 'retained CIM installation'
    script = install(root, 'import time; time.sleep(90)')
    process = subprocess.Popen([sys.executable, str(script)], creationflags=subprocess.CREATE_NO_WINDOW)
    wrapper = tmp_path / 'retained-cim.ps1'
    quoted_root = str(root).replace("'", "''")
    quoted_helper = str(HELPER).replace("'", "''")
    # Only WMI visibility is controlled. Ownership, parsing, actual native
    # termination and exit-wait logic execute against disposable real Python.
    # The old helper loops until timeout because these WMI rows never vanish.
    wrapper.write_text("""$ErrorActionPreference = 'Stop'
$script:Snapshot = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains('""" + quoted_root + """') })
if ($script:Snapshot.Count -eq 0) { throw 'No disposable process found' }
function Get-CimInstance {
    param($ClassName,$Filter)
    if ($Filter -match '^ProcessId=(\\d+)$') {
        return $script:Snapshot | Where-Object { $_.ProcessId -eq [int]$Matches[1] }
    }
    return $script:Snapshot
}
. '""" + quoted_helper + """' -InstallDir '""" + quoted_root + """'
Write-Output 'retained-cim-completed'
""", encoding='utf-8')
    try:
        result = powershell('-File', str(wrapper), timeout=25)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'retained-cim-completed' in result.stdout
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


def test_changed_creation_identity_preserves_real_owned_process(tmp_path):
    root = tmp_path / 'PID identity installation'
    script = install(root, 'import time; time.sleep(90)')
    process = subprocess.Popen([sys.executable, str(script)], creationflags=subprocess.CREATE_NO_WINDOW)
    wrapper = tmp_path / 'changed-identity.ps1'
    quoted_root = str(root).replace("'", "''")
    quoted_helper = str(HELPER).replace("'", "''")
    wrapper.write_text("""$ErrorActionPreference = 'Stop'
$script:Snapshot = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains('""" + quoted_root + """') } |
    Select-Object Name,ProcessId,CommandLine,CreationDate)
if ($script:Snapshot.Count -eq 0) { throw 'No disposable process found' }
function Get-CimInstance {
    param($ClassName,$Filter)
    if ($Filter -match '^ProcessId=(\\d+)$') {
        return $script:Snapshot | Where-Object { $_.ProcessId -eq [int]$Matches[1] } |
            Select-Object Name,ProcessId,CommandLine,@{Name='CreationDate';Expression={$_.CreationDate.AddSeconds(1)}}
    }
    return $script:Snapshot
}
. '""" + quoted_helper + """' -InstallDir '""" + quoted_root + """'
Write-Output 'changed-identity-preserved'
""", encoding='utf-8')
    try:
        result = powershell('-File', str(wrapper), timeout=25)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'changed-identity-preserved' in result.stdout
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
