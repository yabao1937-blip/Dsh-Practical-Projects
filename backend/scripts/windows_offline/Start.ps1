param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$env:PYTHONHOME = $null
$env:PYTHONPATH = $null
$env:PYTHONUTF8 = '1'
$env:DMCS_DATABASE_URL = 'sqlite:///' + (Join-Path $PSScriptRoot 'backend\data\dense_medium.db').Replace('\', '/')
$env:DMCS_TELEMETRY_SPOOL = Join-Path $PSScriptRoot 'backend\data\telemetry-spool'
$env:DMCS_HOST = '0.0.0.0'
$env:DMCS_PORT = '8000'
$taskListener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($taskListener) {
    $taskOwner = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $taskListener.OwningProcess)
    $taskParent = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $taskOwner.ParentProcessId)
    $taskCommands = [string]$taskOwner.CommandLine + ' ' + [string]$taskParent.CommandLine
    if ($taskCommands.IndexOf((Join-Path $PSScriptRoot 'runtime\python.exe'), [StringComparison]::OrdinalIgnoreCase) -lt 0 -and
        $taskCommands.IndexOf((Join-Path $PSScriptRoot 'backend\.venv\Scripts\python.exe'), [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw 'Port 8000 is occupied by another installation. Existing processes were preserved.'
    }
}
& (Join-Path $PSScriptRoot 'backend\scripts\start_server.ps1') -NoBrowser
if ($LASTEXITCODE -ne 0) { throw 'Web service could not start' }
$taskPython = Join-Path $PSScriptRoot 'backend\.venv\Scripts\python.exe'
& $taskPython -X utf8 (Join-Path $PSScriptRoot 'backend\scripts\start_collector.py')
if ($LASTEXITCODE -ne 0) { Write-Warning 'Collector not started. Check the local KEPServer configuration.' }
if (-not $NoBrowser) { Start-Process 'http://127.0.0.1:8000/' }
