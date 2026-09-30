# Start the dense-medium density control system backend (uvicorn) on port 8000.
#
# Detachment matters: this machine has seen the server die three times (2026-09-27/28/30) with a
# bare "^C" as the last log line -- i.e. a console-level CTRL_C sent by whoever launched it,
# not a crash. Start-Process children die with their shell; WMI-created processes still own a
# console and therefore still catch that CTRL_C. So we create the process with
#   DETACHED_PROCESS (no console at all) + CREATE_NEW_PROCESS_GROUP
#   + CREATE_BREAKAWAY_FROM_JOB (leave the caller's job object, so it is not killed with it).
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File backend\scripts\start_server.ps1
#   ... -NoBrowser      # do not open the browser (used by automated checks)
param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)      # backend\scripts -> backend -> repo
$py = Join-Path $repo 'backend\.venv\Scripts\python.exe'
$workdir = Join-Path $repo 'backend'
$log = Join-Path $env:TEMP 'dmcs_server.log'
$port = 8000

function Get-LanUrls {
    # Only adapters that are actually Up, and label each address with its adapter name:
    # this machine also carries WSL/Hyper-V and hotspot adapters whose addresses are NOT
    # reachable from the plant LAN.
    $real, $virtual = @(), @()
    foreach ($ip in (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                     Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' })) {
        $ad = Get-NetAdapter -InterfaceIndex $ip.InterfaceIndex -ErrorAction SilentlyContinue
        if (-not $ad -or $ad.Status -ne 'Up') { continue }
        $isVirtual = $ad.InterfaceDescription -match 'Hyper-V|WSL|Virtual|Loopback'
        $line = "http://$($ip.IPAddress):$port/  ($($ad.Name))"
        if ($isVirtual) { $virtual += $line } else { $real += $line }
    }
    return @($real + $virtual)
}

if (-not (Test-Path $py)) { Write-Host "ERROR: python not found: $py" -ForegroundColor Red; exit 1 }

$listen = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listen) {
    $owner = ($listen | Select-Object -First 1).OwningProcess
    Write-Host "Already running: port $port is listening (PID $owner)." -ForegroundColor Yellow
    Write-Host "  local : http://127.0.0.1:$port/"
    foreach ($u in Get-LanUrls) { Write-Host "  LAN   : $u" }
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$port/" }
    exit 0
}

Write-Host "Starting backend (detached: no console, breaks away from the caller job) ..."
$cmdline = 'cmd.exe /c ""' + $py + '" -m uvicorn app.main:app --host 0.0.0.0 --port ' + $port + ' > "' + $log + '" 2>&1"'

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class DetachedProc {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct STARTUPINFO {
        public int cb; public string lpReserved; public string lpDesktop; public string lpTitle;
        public int dwX; public int dwY; public int dwXSize; public int dwYSize;
        public int dwXCountChars; public int dwYCountChars; public int dwFillAttribute; public int dwFlags;
        public short wShowWindow; public short cbReserved2; public IntPtr lpReserved2;
        public IntPtr hStdInput; public IntPtr hStdOutput; public IntPtr hStdError;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct PROCESS_INFORMATION { public IntPtr hProcess; public IntPtr hThread; public int dwProcessId; public int dwThreadId; }
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    public static extern bool CreateProcess(string lpApplicationName, string lpCommandLine,
        IntPtr lpProcessAttributes, IntPtr lpThreadAttributes, bool bInheritHandles, uint dwCreationFlags,
        IntPtr lpEnvironment, string lpCurrentDirectory, ref STARTUPINFO si, out PROCESS_INFORMATION pi);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(IntPtr h);
}
"@

$si = New-Object DetachedProc+STARTUPINFO
$si.cb = [System.Runtime.InteropServices.Marshal]::SizeOf($si)
$pi = New-Object DetachedProc+PROCESS_INFORMATION
$DETACHED = 0x00000008
$NEWGROUP = 0x00000200
$NOWINDOW = 0x08000000
$BREAKAWAY = 0x01000000
$cmdExe = Join-Path $env:SystemRoot 'System32\cmd.exe'
$flags = $DETACHED -bor $NEWGROUP -bor $NOWINDOW -bor $BREAKAWAY
$ok = [DetachedProc]::CreateProcess($cmdExe, $cmdline, [IntPtr]::Zero, [IntPtr]::Zero, $false, $flags,
                                    [IntPtr]::Zero, $workdir, [ref]$si, [ref]$pi)
if (-not $ok) {
    Write-Host "  (breakaway rejected by the caller job, retrying without it)" -ForegroundColor Yellow
    $flags = $DETACHED -bor $NEWGROUP -bor $NOWINDOW
    $ok = [DetachedProc]::CreateProcess($cmdExe, $cmdline, [IntPtr]::Zero, [IntPtr]::Zero, $false, $flags,
                                        [IntPtr]::Zero, $workdir, [ref]$si, [ref]$pi)
}
if (-not $ok) {
    $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
    Write-Host "ERROR: CreateProcess failed (win32=$err)" -ForegroundColor Red
    exit 1
}
[void][DetachedProc]::CloseHandle($pi.hProcess)
[void][DetachedProc]::CloseHandle($pi.hThread)
Write-Host "  launched (PID $($pi.dwProcessId), no console), log: $log"

$healthy = $false
for ($i = 1; $i -le 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $code = (Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/v1/health" -UseBasicParsing -TimeoutSec 3).StatusCode
        if ($code -eq 200) { $healthy = $true; break }
    } catch { }
}
if ($healthy) {
    Write-Host "OK: backend is up after $i second(s)." -ForegroundColor Green
    Write-Host "  local : http://127.0.0.1:$port/"
    foreach ($u in Get-LanUrls) { Write-Host "  LAN   : $u" }
    Write-Host "Stop it with: backend\scripts\stop_server.ps1 (or the stop .bat)"
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$port/" }
    exit 0
}
Write-Host "ERROR: backend did not answer /api/v1/health within 30s." -ForegroundColor Red
Write-Host "Last lines of ${log}:"
if (Test-Path $log) { Get-Content $log -Tail 20 }
exit 1
