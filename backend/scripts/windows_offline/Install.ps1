param([string]$InstallDir = (Join-Path $env:USERPROFILE 'DshDensityControl'), [switch]$NoStart, [switch]$NoShortcuts)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONHOME = $null
$env:PYTHONPATH = $null
$env:PYTHONUTF8 = '1'
function Copy-PointMetadata([string]$Source, [string]$Destination) {
    if (-not (Test-Path -LiteralPath $Source)) { return }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    foreach ($taskMetadata in Get-ChildItem -LiteralPath $Source -File -Filter '*.json') {
        if ($taskMetadata.BaseName -notmatch '^[0-9a-f]{64}$' -or
            ($taskMetadata.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Invalid point metadata file.' }
        if ((Get-FileHash -LiteralPath $taskMetadata.FullName -Algorithm SHA256).Hash -ne $taskMetadata.BaseName) {
            throw 'Point metadata checksum failed.'
        }
        $taskMetadataTarget = Join-Path $Destination $taskMetadata.Name
        if (-not (Test-Path -LiteralPath $taskMetadataTarget)) {
            [IO.File]::Copy($taskMetadata.FullName, $taskMetadataTarget, $false)
        }
        if ((Get-FileHash -LiteralPath $taskMetadataTarget -Algorithm SHA256).Hash -ne $taskMetadata.BaseName) {
            throw 'Existing point metadata conflicts with the installation snapshot.'
        }
    }
}
function Send-InstallStatus([string]$stage, [string]$message = '', [bool]$healthy = $false) {
    if (-not $taskManifest.report_url -or $NoStart) { return }
    try {
        $taskReport = @{stage=$stage;message=$message;healthy=$healthy;revision=$taskManifest.revision;at=(Get-Date).ToString('o')} | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri $taskManifest.report_url -Method Post -ContentType 'application/json' -Body $taskReport -TimeoutSec 5 | Out-Null
    } catch { Write-Warning 'Could not send installation progress to the laptop.' }
}
trap { Send-InstallStatus 'failed' $_.Exception.Message; Write-Host ('INSTALL FAILED: ' + $_.Exception.Message) -ForegroundColor Red; exit 1 }
$taskRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$taskInstall = [System.IO.Path]::GetFullPath($InstallDir)
if (-not [Environment]::Is64BitOperatingSystem) { throw 'This package requires 64-bit Windows.' }
if ($taskInstall.StartsWith($taskRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or $taskInstall -eq $taskRoot) {
    throw 'Choose an installation directory outside the extracted package.'
}
$taskManifest = Get-Content -LiteralPath (Join-Path $taskRoot 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Write-Host '1/5 Verifying package integrity...'
Send-InstallStatus 'verifying'
foreach ($taskEntry in $taskManifest.files.PSObject.Properties) {
    $taskFile = [System.IO.Path]::GetFullPath((Join-Path $taskRoot $taskEntry.Name))
    if (-not $taskFile.StartsWith($taskRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe manifest path.' }
    if ((Get-FileHash -LiteralPath $taskFile -Algorithm SHA256).Hash -ne $taskEntry.Value) { throw ('Checksum failed: ' + $taskEntry.Name) }
}
$taskMarkerPath = Join-Path $taskInstall 'offline-install.json'
if (Test-Path -LiteralPath $taskInstall) {
    if (-not (Test-Path -LiteralPath $taskMarkerPath)) { throw 'Installation directory already exists. Choose a NEW empty directory.' }
    $taskPrevious = Get-Content -LiteralPath $taskMarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($taskPrevious.created_at -ne $taskManifest.created_at) { throw 'A different bundle was already installed here. Existing data was preserved.' }
} else { New-Item -ItemType Directory -Path $taskInstall | Out-Null }
@{created_at=$taskManifest.created_at;revision=$taskManifest.revision;state='installing'} | ConvertTo-Json | Set-Content -LiteralPath $taskMarkerPath -Encoding UTF8
Write-Host '2/5 Copying application and portable Python...'
Send-InstallStatus 'copying'
foreach ($taskDirectory in Get-ChildItem -LiteralPath (Join-Path $taskRoot 'project')) {
    Copy-Item -LiteralPath $taskDirectory.FullName -Destination $taskInstall -Recurse -Force
}
if (-not (Test-Path -LiteralPath (Join-Path $taskInstall 'runtime\python.exe'))) {
    Copy-Item -LiteralPath (Join-Path $taskRoot 'runtime') -Destination $taskInstall -Recurse
}
$taskDatabase = Join-Path $taskInstall 'backend\data\dense_medium.db'
# Publish immutable identity versions before the database, including resumed installs.
& (Join-Path $taskInstall 'runtime\python.exe') -I -c "import sys; sys.path.insert(0,sys.argv[1]); from point_metadata import validate_catalog_presence; validate_catalog_presence(sys.argv[2])" (Join-Path $taskInstall 'backend') (Join-Path $taskRoot 'snapshot\dense_medium.db')
if ($LASTEXITCODE -ne 0) { throw 'Snapshot requires missing point metadata; database was not installed.' }
Copy-PointMetadata -Source (Join-Path $taskRoot 'snapshot\point-metadata') -Destination (Join-Path $taskInstall 'backend\data\point-metadata')
if (-not (Test-Path -LiteralPath $taskDatabase)) { Copy-Item -LiteralPath (Join-Path $taskRoot 'snapshot\dense_medium.db') -Destination $taskDatabase }
$taskConfig = Join-Path $taskInstall 'backend\config\kepserver.local.json'
if ((Test-Path -LiteralPath (Join-Path $taskRoot 'configuration\kepserver.local.json')) -and -not (Test-Path -LiteralPath $taskConfig)) {
    Copy-Item -LiteralPath (Join-Path $taskRoot 'configuration\kepserver.local.json') -Destination $taskConfig
    if (Test-Path -LiteralPath (Join-Path $taskRoot 'configuration\certs')) {
        Copy-Item -LiteralPath (Join-Path $taskRoot 'configuration\certs') -Destination (Split-Path -Parent $taskConfig) -Recurse -Force
    }
}
$taskBase = Join-Path $taskInstall 'runtime\python.exe'
$taskVenv = Join-Path $taskInstall 'backend\.venv'
$taskPython = Join-Path $taskVenv 'Scripts\python.exe'
Write-Host '3/5 Creating a NEW local Python environment...'
Send-InstallStatus 'dependencies'
& $taskBase -I -m venv $taskVenv
if ($LASTEXITCODE -ne 0) { throw 'Could not create local Python environment.' }
& $taskPython -m pip install --no-index --disable-pip-version-check --find-links (Join-Path $taskRoot 'wheels') -r (Join-Path $taskInstall 'backend\requirements-kepserver.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Offline dependency installation failed.' }
& $taskPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed.' }
& $taskPython -I -c "import asyncua,fastapi,uvicorn,sqlalchemy,numpy,scipy.linalg,sklearn,cryptography,ssl,sqlite3; print('Runtime imports OK')"
if ($LASTEXITCODE -ne 0) { throw 'Runtime import check failed on this computer.' }
Write-Host '4/5 Creating start shortcuts...'
Copy-Item -LiteralPath (Join-Path $taskRoot 'Start.ps1') -Destination $taskInstall -Force
if (-not $NoShortcuts) {
    $taskShell = New-Object -ComObject WScript.Shell
    foreach ($taskFolder in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Startup'))) {
        $taskShortcut = $taskShell.CreateShortcut((Join-Path $taskFolder 'DshDensityControl.lnk'))
        $taskShortcut.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $taskArguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + (Join-Path $taskInstall 'Start.ps1') + '"'
        if ($taskFolder -eq [Environment]::GetFolderPath('Startup')) { $taskArguments += ' -NoBrowser' }
        $taskShortcut.Arguments = $taskArguments
        $taskShortcut.WorkingDirectory = $taskInstall
        $taskShortcut.Save()
    }
}
@{created_at=$taskManifest.created_at;revision=$taskManifest.revision;state='installed';installed_at=(Get-Date).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $taskMarkerPath -Encoding UTF8
Write-Host ('5/5 Installed: ' + $taskInstall)
if (-not $NoStart) {
    Send-InstallStatus 'starting'
    & (Join-Path $taskInstall 'Start.ps1')
    $taskHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/v1/health' -TimeoutSec 15
    if ($taskHealth.status -ne 'ok') { throw 'Installed service health check failed.' }
    Send-InstallStatus 'installed' 'Local service is healthy. Connect the production network for KEPServer data.' $true
}
Write-Host 'Open http://127.0.0.1:8000/ on THIS computer. KEPServer data requires the production network cable.'
