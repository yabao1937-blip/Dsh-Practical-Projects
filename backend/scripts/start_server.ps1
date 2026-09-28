# Start the dense-medium density control system backend (uvicorn) on port 8000.
#
# Why WMI (Invoke-CimMethod Win32_Process Create) instead of Start-Process:
#   a process started by Start-Process is a CHILD of the shell that ran it, so it dies
#   together with that shell (Windows job object). Processes created through WMI belong
#   to the WMI service, so the server keeps running after this window closes.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File backend\scripts\start_server.ps1
#   ... -NoBrowser   # do not open the browser (used by automated checks)
param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)      # backend\scripts -> backend -> repo
$py = Join-Path $repo 'backend\.venv\Scripts\python.exe'
$workdir = Join-Path $repo 'backend'
$log = Join-Path $env:TEMP 'dmcs_server.log'
$port = 8000

function Get-LanUrls {
    # Only adapters that are actually Up, and say which adapter each address belongs to:
    # the machine also carries WSL/Hyper-V and hotspot adapters whose addresses are NOT
    # reachable from the plant LAN (a wrong URL here wastes the operator's time).
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
    Write-Host ("  local : http://127.0.0.1:$port/")
    foreach ($u in Get-LanUrls) { Write-Host "  LAN   : $u" }
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$port/" }
    exit 0
}

Write-Host "Starting backend (detached via WMI) ..."
$cmd = 'cmd.exe /c ""' + $py + '" -m uvicorn app.main:app --host 0.0.0.0 --port ' + $port + ' > "' + $log + '" 2>&1"'
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine      = $cmd
    CurrentDirectory = $workdir
}
if ($r.ReturnValue -ne 0) { Write-Host "ERROR: WMI create failed (ReturnValue=$($r.ReturnValue))" -ForegroundColor Red; exit 1 }
Write-Host "  launched (PID $($r.ProcessId)), log: $log"

$ok = $false
for ($i = 1; $i -le 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $code = (Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/v1/health" -UseBasicParsing -TimeoutSec 3).StatusCode
        if ($code -eq 200) { $ok = $true; break }
    } catch { }
}
if ($ok) {
    Write-Host "OK: backend is up after $i second(s)." -ForegroundColor Green
    Write-Host ("  local : http://127.0.0.1:$port/")
    foreach ($u in Get-LanUrls) { Write-Host "  LAN   : $u" }
    Write-Host "Stop it with: backend\scripts\stop_server.ps1  (or the stop .bat)"
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$port/" }
    exit 0
} else {
    Write-Host "ERROR: backend did not answer /api/v1/health within 30s." -ForegroundColor Red
    Write-Host "Last lines of $log :"
    if (Test-Path $log) { Get-Content $log -Tail 20 }
    exit 1
}
