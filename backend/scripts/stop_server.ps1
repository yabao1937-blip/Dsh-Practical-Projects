# Stop the dense-medium density control system backend (whatever listens on port 8000).
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File backend\scripts\stop_server.ps1
param([int]$Port = 8000)

$ErrorActionPreference = 'Stop'
$listen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $listen) { Write-Host "Not running: nothing is listening on port $Port." -ForegroundColor Yellow; exit 0 }

$pids = $listen | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($procId in $pids) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
    $name = if ($p) { $p.Name } else { 'unknown' }
    Write-Host "Stopping PID $procId ($name) ..."
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
$still = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($still) { Write-Host "WARNING: port $Port is still listening." -ForegroundColor Red; exit 1 }
Write-Host "OK: backend stopped (port $Port free)." -ForegroundColor Green
exit 0
