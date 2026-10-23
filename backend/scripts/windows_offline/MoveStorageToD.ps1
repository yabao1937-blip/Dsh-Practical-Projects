param(
    [string]$InstallDir = (Join-Path $env:USERPROFILE 'DshDensityControl'),
    [string]$DestinationRoot = 'D:\DshDensityControlData',
    [switch]$FullCheck,
    [switch]$StopOwnedWatchdog,
    [switch]$RestartOwnedWatchdog,
    [switch]$CreateJunction,
    [string]$SourcePath,
    [string]$TargetPath,
    [int]$WatchdogInterval = 60
)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
if (-not (Test-Path -LiteralPath (Join-Path $taskRoot 'offline-install.json'))) { throw 'Unrecognized installation.' }
if ($CreateJunction) {
    $taskSource = [IO.Path]::GetFullPath($SourcePath)
    $taskAllowed = @((Join-Path $taskRoot 'backend\data'), (Join-Path $taskRoot 'backups'))
    if ($taskSource -notin $taskAllowed -or (Test-Path -LiteralPath $taskSource)) { throw 'Unexpected or existing junction source.' }
    $taskTarget = Get-Item -LiteralPath $TargetPath -Force
    if (-not $taskTarget.PSIsContainer -or ($taskTarget.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Junction target must be an ordinary directory.' }
    New-Item -ItemType Junction -Path $taskSource -Target $taskTarget.FullName | Out-Null
    exit 0
}
if ($StopOwnedWatchdog) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class StorageCommandLine {
    [DllImport("shell32.dll", SetLastError=true)] private static extern IntPtr CommandLineToArgvW([MarshalAs(UnmanagedType.LPWStr)] string text, out int count);
    [DllImport("kernel32.dll")] private static extern IntPtr LocalFree(IntPtr memory);
    public static string[] Parse(string text) {
        int count; IntPtr memory = CommandLineToArgvW(text, out count);
        if (memory == IntPtr.Zero) throw new InvalidOperationException("Cannot parse process arguments");
        try { string[] args = new string[count]; for (int i=0; i<count; i++) args[i]=Marshal.PtrToStringUni(Marshal.ReadIntPtr(memory,i*IntPtr.Size)); return args; }
        finally { LocalFree(memory); }
    }
}
'@
    $taskScript = Join-Path $taskRoot 'backend\scripts\watchdog_server.ps1'
    function Get-OwnedInterval($taskProcess) {
        if (-not $taskProcess.CommandLine) { return $null }
        $taskArgs = [StorageCommandLine]::Parse([string]$taskProcess.CommandLine)
        $taskIndex = [Array]::FindIndex($taskArgs, [Predicate[string]]{ param($value) $value -ieq '-File' })
        if ($taskIndex -lt 0 -or $taskIndex+1 -ge $taskArgs.Length) { return $null }
        $taskFile = $taskArgs[$taskIndex+1]
        $taskFullyQualified = $taskFile -match '^[A-Za-z]:[\\/]' -or $taskFile -match '^\\\\[^\\]+\\[^\\]+\\'
        if (-not $taskFullyQualified) {
            if ([IO.Path]::GetFileName($taskFile) -ieq 'watchdog_server.ps1') {
                throw 'A relative-path watchdog_server.ps1 is running. Its installation cannot be proved. Close that watchdog window before migrating; no process was stopped.'
            }
            return $null
        }
        if (-not [IO.Path]::GetFullPath($taskFile).Equals($taskScript,[StringComparison]::OrdinalIgnoreCase)) { return $null }
        $taskInterval = 60
        for ($taskNext=$taskIndex+2; $taskNext+1 -lt $taskArgs.Length; $taskNext++) {
            if ($taskArgs[$taskNext] -ieq '-IntervalSec') {
                if (-not [int]::TryParse($taskArgs[$taskNext+1],[ref]$taskInterval) -or $taskInterval -lt 1 -or $taskInterval -gt 86400) { throw 'Invalid owned watchdog interval.' }
            }
        }
        return $taskInterval
    }
    $taskIntervals = @()
    $taskCandidates = @()
    # Complete the ambiguity check before stopping any watchdog. A relative
    # -File invocation has no trustworthy working directory in Win32_Process.
    foreach ($taskCandidate in @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'")) {
        $taskInterval = Get-OwnedInterval $taskCandidate
        if ($null -eq $taskInterval) { continue }
        $taskCandidates += @{process=$taskCandidate;interval=$taskInterval}
    }
    foreach ($taskEntry in $taskCandidates) {
        $taskCandidate = $taskEntry.process
        $taskInterval = $taskEntry.interval
        $taskCurrent = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $taskCandidate.ProcessId)
        if (-not $taskCurrent -or $taskCurrent.CreationDate -ne $taskCandidate.CreationDate -or $null -eq (Get-OwnedInterval $taskCurrent)) { continue }
        Stop-Process -Id $taskCurrent.ProcessId -Force -ErrorAction Stop
        Wait-Process -Id $taskCurrent.ProcessId -Timeout 60 -ErrorAction SilentlyContinue
        $taskRemaining = Get-Process -Id $taskCurrent.ProcessId -ErrorAction SilentlyContinue
        if ($taskRemaining -and -not $taskRemaining.HasExited) { throw 'Owned watchdog has not exited.' }
        $taskIntervals += $taskInterval
    }
    @{intervals=@($taskIntervals)} | ConvertTo-Json -Compress
    exit 0
}
if ($RestartOwnedWatchdog) {
    if ($WatchdogInterval -lt 1 -or $WatchdogInterval -gt 86400) { throw 'Invalid watchdog interval.' }
    $taskArguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $taskRoot 'backend\scripts\watchdog_server.ps1') + '" -IntervalSec ' + $WatchdogInterval
    Start-Process powershell.exe -ArgumentList $taskArguments -WorkingDirectory $taskRoot -WindowStyle Hidden
    exit 0
}
$env:PYTHONHOME = $null
$env:PYTHONPATH = $null
$taskPython = Join-Path $taskRoot 'backend\.venv\Scripts\python.exe'
$taskArguments = @('-X','utf8',(Join-Path $PSScriptRoot 'move_storage.py'),'--install-dir',$taskRoot,'--destination-root',$DestinationRoot)
if ($FullCheck) { $taskArguments += '--full-check' }
& $taskPython @taskArguments
exit $LASTEXITCODE
