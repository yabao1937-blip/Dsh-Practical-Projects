param([Parameter(Mandatory=$true)][string]$InstallDir)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
if (-not (Test-Path -LiteralPath (Join-Path $taskRoot 'offline-install.json'))) { throw 'Not a recognized installation.' }
$taskListener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $taskListener) { exit 0 }
$taskProcess = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $taskListener.OwningProcess)
$taskParent = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $taskProcess.ParentProcessId)
function Is-OwnedWeb($taskCandidate) {
    return $taskCandidate -and $taskCandidate.Name -match '^python(w)?\.exe$' -and
        ([string]$taskCandidate.CommandLine).IndexOf($taskRoot + '\', [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        ([string]$taskCandidate.CommandLine) -match 'serve\.py'
}
if (-not (Is-OwnedWeb $taskProcess) -and -not (Is-OwnedWeb $taskParent)) { throw 'Port 8000 belongs to another installation; nothing was stopped.' }
Stop-Process -Id $taskProcess.ProcessId -Force
if (Is-OwnedWeb $taskParent) { Stop-Process -Id $taskParent.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host 'Owned web process stopped. Production collector was preserved.'
