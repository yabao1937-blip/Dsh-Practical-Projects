param(
    [string]$InstallDir = (Join-Path $env:USERPROFILE 'DshDensityControl'),
    [string]$DestinationRoot = 'D:\DshDensityControlData',
    [switch]$ConfirmRemoval
)
$ErrorActionPreference = 'Stop'
$env:PYTHONHOME = $null
$env:PYTHONPATH = $null
$taskRoot = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$taskArguments = @('-X','utf8',(Join-Path $PSScriptRoot 'move_storage.py'),'--install-dir',$taskRoot,'--destination-root',$DestinationRoot,'--finalize')
if ($ConfirmRemoval) { $taskArguments += '--confirm-removal' }
& (Join-Path $taskRoot 'backend\.venv\Scripts\python.exe') @taskArguments
exit $LASTEXITCODE
