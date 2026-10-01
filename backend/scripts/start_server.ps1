# Start the backend (uvicorn :8000) detached: no console, no inherited handles.
#
# Order of attempts (changed 2026-09-30 after measuring this machine):
#   1) direct CreateProcess + DETACHED_PROCESS -- works everywhere;
#   2) WMI bootstrap -> powershell -Inner          -- only if (1) fails. Kept for machines where
#      the caller's job object cannot be left any other way. NOTE: on THIS machine "WMI ->
#      powershell" is denied ("拒绝访问", exit 5) by some resident security/cleanup software
#      (Tyty x4 and friends), while "WMI -> cmd" works -- so (2) is a fallback, not the default.
# The server itself: backend/serve.py writes its own log (UTF-8+BOM) and runs uvicorn.
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File backend\scripts\start_server.ps1 [-NoBrowser]
param([switch]$NoBrowser, [switch]$Inner)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)      # backend\scripts -> backend -> repo
$py = Join-Path $repo 'backend\.venv\Scripts\python.exe'
$workdir = Join-Path $repo 'backend'
$log = Join-Path $env:TEMP 'dmcs_server.log'
$port = 8000

function Get-LanUrls {
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

function Wait-Healthy([int]$seconds) {
    for ($i = 1; $i -le $seconds; $i++) {
        Start-Sleep -Seconds 1
        try {
            $code = (Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/v1/health" -UseBasicParsing -TimeoutSec 3).StatusCode
            if ($code -eq 200) { return $true }
        } catch { }
    }
    return $false
}

function Show-Up([string]$how) {
    $listen = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    $owner = if ($listen) { ($listen | Select-Object -First 1).OwningProcess } else { '?' }
    Write-Host "OK: backend is up ($how; listening PID $owner)." -ForegroundColor Green
    Write-Host "  local : http://127.0.0.1:$port/"
    foreach ($u in Get-LanUrls) { Write-Host "  LAN   : $u" }
    Write-Host "  log   : $log"
    Write-Host "Stop it with: backend\scripts\stop_server.ps1 (or the stop .bat)"
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$port/" }
}

# ---------------- launch code (used both directly and by the WMI bootstrap) ----------------
function Start-DetachedProcess {
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
    $env:DMCS_LOG = $log
    $env:DMCS_PORT = "$port"
    $cmdline = '"' + $py + '" -u serve.py'
    # bInheritHandles = $false: inherit nothing. With $true the child also inherits the caller's
    # stdout pipe and the caller then waits until the server exits (looks like "the script hangs").
    $flags = 0x00000008 -bor 0x00000200 -bor 0x08000000     # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    if (-not [DetachedProc]::CreateProcess($py, $cmdline, [IntPtr]::Zero, [IntPtr]::Zero, $false, $flags,
                                           [IntPtr]::Zero, $workdir, [ref]$si, [ref]$pi)) {
        throw ("CreateProcess failed, win32=" + [System.Runtime.InteropServices.Marshal]::GetLastWin32Error())
    }
    [void][DetachedProc]::CloseHandle($pi.hProcess)
    [void][DetachedProc]::CloseHandle($pi.hThread)
    return $pi.dwProcessId
}

if (-not (Test-Path $py)) { Write-Host "ERROR: python not found: $py" -ForegroundColor Red; exit 1 }

# WMI bootstrap mode: this process was started by WMI, just launch and exit.
if ($Inner) {
    try {
        $pid2 = Start-DetachedProcess
        Write-Output ("INNER-OK pid=" + $pid2)
        exit 0
    } catch {
        Write-Output ("INNER-FAIL " + $_.Exception.Message)
        exit 1
    }
}

$listen = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listen) {
    $owner = ($listen | Select-Object -First 1).OwningProcess
    Write-Host "Already running: port $port is listening (PID $owner)." -ForegroundColor Yellow
    Write-Host "  local : http://127.0.0.1:$port/"
    foreach ($u in Get-LanUrls) { Write-Host "  LAN   : $u" }
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$port/" }
    exit 0
}

Write-Host "Starting backend (direct, detached: no console, no inherited handles) ..."
try {
    $launchedPid = Start-DetachedProcess
    Write-Host "  launched (PID $launchedPid)"
} catch {
    Write-Host ("  direct launch failed: " + $_.Exception.Message) -ForegroundColor Yellow
}
if (Wait-Healthy 15) { Show-Up 'direct'; exit 0 }

Write-Host "  not healthy yet; trying the WMI bootstrap ..." -ForegroundColor Yellow
$innerCmd = 'powershell -NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Inner'
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine      = $innerCmd
    CurrentDirectory = $workdir
}
if ($r.ReturnValue -ne 0) { Write-Host "  WMI bootstrap failed (ReturnValue=$($r.ReturnValue))" -ForegroundColor Yellow }
if (Wait-Healthy 20) { Show-Up 'WMI bootstrap'; exit 0 }

Write-Host "ERROR: backend did not answer /api/v1/health." -ForegroundColor Red
Write-Host "Last lines of ${log}:"
if (Test-Path $log) { Get-Content $log -Tail 20 -Encoding utf8 }
exit 1
