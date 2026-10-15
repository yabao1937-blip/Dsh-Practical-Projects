param([string]$InstallDir = (Join-Path $env:USERPROFILE 'DshDensityControl'))
$ErrorActionPreference = 'Stop'
function Get-UpdateHash([string]$Path) {
    $taskStream = [System.IO.File]::OpenRead($Path)
    $taskHasher = [System.Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($taskHasher.ComputeHash($taskStream)).Replace('-', '') }
    finally { $taskHasher.Dispose(); $taskStream.Dispose() }
}
try {
    $taskTarget = Join-Path $InstallDir 'frontend\js\api.js'
    if (-not (Test-Path -LiteralPath (Join-Path $InstallDir 'offline-install.json')) -or
            -not (Test-Path -LiteralPath $taskTarget -PathType Leaf)) {
        throw 'Cannot find the installed DshDensityControl application for this Windows user.'
    }
    $taskManifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'browser-compat.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $taskPayload = Join-Path $PSScriptRoot 'frontend\js\api.js'
    if ((Get-UpdateHash $taskPayload) -ne $taskManifest.sha256) {
        throw 'Update package verification failed. Extract the complete ZIP and try again.'
    }
    $taskCurrent = Get-UpdateHash $taskTarget
    if ($taskCurrent -eq $taskManifest.sha256) {
        Write-Host 'Browser compatibility update is already installed. Press Ctrl+F5 in the browser.' -ForegroundColor Green
        exit 0
    }
    if ($taskCurrent -ne $taskManifest.previous_sha256) {
        throw 'The installed frontend differs from this update version. No files were changed.'
    }
    $taskTemporary = $taskTarget + '.browser-compat.tmp'
    $taskBackup = $taskTarget + '.before-browser-compat-' + (Get-Date -Format 'yyyyMMddHHmmssfff') + '.bak'
    Copy-Item -LiteralPath $taskPayload -Destination $taskTemporary -Force
    [System.IO.File]::Replace($taskTemporary, $taskTarget, $taskBackup)
    Write-Host 'Browser compatibility update installed. Press Ctrl+F5 in the browser.' -ForegroundColor Green
    Write-Host ('Previous file saved at: ' + $taskBackup)
    exit 0
} catch {
    Write-Host ('UPDATE FAILED: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
