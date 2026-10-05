# Keep the DMCS backend alive: check the port every N seconds, start it if it is gone.
#
# Why this exists: on this machine the server has been found dead 9 times. Evidence gathered so far:
#   * the process is terminated hard -- serve.py never gets to write its "exit" line;
#   * it is NOT a reboot (uptime 276 h at the time of writing) and NOT Huorong AV (no log entries);
#   * killers stay unidentified (several agent runtimes / proxy clients are resident here).
# So instead of guessing the killer, this script simply brings the service back within a minute.
# It is a plain window you start yourself: no scheduled task, no registry change, no admin rights.
# Closing the window stops the watchdog (and leaves the server running).
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File backend\scripts\watchdog_server.ps1
#   ... -IntervalSec 30     # check every 30 s (default 60)
param([int]$IntervalSec = 60)

$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$starter = Join-Path $PSScriptRoot 'start_server.ps1'
$port = 8000
$log = Join-Path $env:TEMP 'dmcs_watchdog.log'

function Say([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

function Test-Up {
    try {
        return (Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/v1/health" -UseBasicParsing -TimeoutSec 4).StatusCode -eq 200
    } catch { return $false }
}

Say "watchdog started (interval ${IntervalSec}s). Close this window to stop it."
$restarts = 0
while ($true) {
    if (Test-Up) {
        Start-Sleep -Seconds $IntervalSec
        continue
    }
    $restarts++
    Say "service is DOWN -> restarting (attempt #$restarts) ..."
    try {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $starter -NoBrowser | Out-Null
    } catch {
        Say ("start_server.ps1 failed: " + $_.Exception.Message)
    }
    Start-Sleep -Seconds 5
    if (Test-Up) { Say "service is back up." } else { Say "still down; will retry in ${IntervalSec}s." }
    Start-Sleep -Seconds $IntervalSec
}
