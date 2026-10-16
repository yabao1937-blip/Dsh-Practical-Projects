param([Parameter(Mandatory=$true)][string]$InstallDir)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
if (-not (Test-Path -LiteralPath (Join-Path $taskRoot 'offline-install.json'))) { throw 'Not a recognized installation.' }
$taskListener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
function Is-OwnedWeb($taskCandidate) {
    return $taskCandidate -and $taskCandidate.Name -match '^python(w)?\.exe$' -and
        ([string]$taskCandidate.CommandLine).IndexOf($taskRoot + '\', [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        ([string]$taskCandidate.CommandLine) -match 'serve\.py'
}
if ($taskListener) {
    $taskProcess = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $taskListener.OwningProcess)
    $taskParent = if ($taskProcess) { Get-CimInstance Win32_Process -Filter ('ProcessId=' + $taskProcess.ParentProcessId) }
    if (-not (Is-OwnedWeb $taskProcess) -and -not (Is-OwnedWeb $taskParent)) { throw 'Port 8000 belongs to another installation; nothing was stopped.' }
}
# A stalled startup or accept loop may leave an owned Python process without a
# listener. Stop only this installation's explicit serve.py processes as well.
$taskOwned = @(Get-CimInstance Win32_Process | Where-Object { Is-OwnedWeb $_ })
foreach ($taskCandidate in $taskOwned) {
    $taskCurrent = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $taskCandidate.ProcessId)
    if ((Is-OwnedWeb $taskCurrent) -and $taskCurrent.CreationDate -eq $taskCandidate.CreationDate) {
        Stop-Process -Id $taskCurrent.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
Write-Host 'Owned web process stopped. Production collector was preserved.'
