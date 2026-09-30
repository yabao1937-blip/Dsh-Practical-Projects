# Start the backend (uvicorn :8000) so that it survives the caller's shell AND its job object.
# Two stages -- see the comment block in backend/scripts/_note_start_design.txt for the full story:
#   outer : WMI creates a powershell  (parent = WmiPrvSE -> outside the caller's job object)
#   inner : that powershell CreateProcess()es the server DETACHED (no console) + NEW_PROCESS_GROUP
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File backend\scripts\start_server.ps1 [-NoBrowser]
param([switch]$NoBrowser, [switch]$Inner)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)      # backend\scripts -> backend -> repo
$py = Join-Path $repo 'backend\.venv\Scripts\python.exe'
$workdir = Join-Path $repo 'backend'
$log = Join-Path $env:TEMP 'dmcs_server.log'
$errlog = Join-Path $env:TEMP 'dmcs_server.err.log'
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

if (-not (Test-Path $py)) { Write-Host "ERROR: python not found: $py" -ForegroundColor Red; exit 1 }

# ---------------- inner stage: runs inside the WMI-created powershell (no job object) ----------------
if ($Inner) {
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
    # 直接起 python（不经 cmd）：DETACHED_PROCESS 下 cmd 的 "> log" 重定向拿不到 uvicorn 输出
    # （2026-09-30 实测两个日志文件都是 0 字节），所以日志交给 backend/serve.py 自己写文件。
    $env:DMCS_LOG = $log
    $env:DMCS_PORT = "$port"
    $cmdline = '"' + $py + '" -u serve.py'
    $flags = 0x00000008 -bor 0x00000200 -bor 0x08000000     # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    if (-not [DetachedProc]::CreateProcess($py, $cmdline, [IntPtr]::Zero, [IntPtr]::Zero, $true, $flags,
                                           [IntPtr]::Zero, $workdir, [ref]$si, [ref]$pi)) {
        Write-Output ("INNER-FAIL win32=" + [System.Runtime.InteropServices.Marshal]::GetLastWin32Error())
        exit 1
    }
    [void][DetachedProc]::CloseHandle($pi.hProcess)
    [void][DetachedProc]::CloseHandle($pi.hThread)
    Write-Output ("INNER-OK pid=" + $pi.dwProcessId)
    exit 0
}

# ---------------- outer stage ----------------
$listen = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listen) {
    $owner = ($listen | Select-Object -First 1).OwningProcess
    Write-Host "Already running: port $port is listening (PID $owner)." -ForegroundColor Yellow
    Write-Host "  local : http://127.0.0.1:$port/"
    foreach ($u in Get-LanUrls) { Write-Host "  LAN   : $u" }
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$port/" }
    exit 0
}

Write-Host "Starting backend (WMI bootstrap -> detached, no console, outside the caller job) ..."
$innerCmd = 'powershell -NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Inner'
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine      = $innerCmd
    CurrentDirectory = $workdir
}
if ($r.ReturnValue -ne 0) { Write-Host "ERROR: WMI bootstrap failed (ReturnValue=$($r.ReturnValue))" -ForegroundColor Red; exit 1 }
Write-Host "  bootstrap ok; waiting for health ..."

$healthy = $false
for ($i = 1; $i -le 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $code = (Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/v1/health" -UseBasicParsing -TimeoutSec 3).StatusCode
        if ($code -eq 200) { $healthy = $true; break }
    } catch { }
}
if ($healthy) {
    $listen = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    $owner = if ($listen) { ($listen | Select-Object -First 1).OwningProcess } else { '?' }
    Write-Host "OK: backend is up after $i second(s) (listening PID $owner)." -ForegroundColor Green
    Write-Host "  local : http://127.0.0.1:$port/"
    foreach ($u in Get-LanUrls) { Write-Host "  LAN   : $u" }
    Write-Host "Stop it with: backend\scripts\stop_server.ps1 (or the stop .bat)"
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$port/" }
    exit 0
}
Write-Host "ERROR: backend did not answer /api/v1/health within 30s." -ForegroundColor Red
Write-Host "Last lines of ${log}:"
if (Test-Path $log) { Get-Content $log -Tail 20 }
Write-Host "Last lines of ${errlog}:"
if (Test-Path $errlog) { Get-Content $errlog -Tail 20 }
exit 1
