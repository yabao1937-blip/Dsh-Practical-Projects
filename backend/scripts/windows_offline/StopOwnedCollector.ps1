param([Parameter(Mandatory=$true)][string]$InstallDir)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
if (-not (Test-Path -LiteralPath (Join-Path $taskRoot 'offline-install.json'))) { throw 'Not a recognized installation.' }
$taskCollector = [IO.Path]::GetFullPath((Join-Path $taskRoot 'backend\collector.py'))
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CollectorCommandLine {
    [DllImport("shell32.dll", SetLastError=true)]
    private static extern IntPtr CommandLineToArgvW([MarshalAs(UnmanagedType.LPWStr)] string text, out int count);
    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr memory);
    public static string[] Parse(string text) {
        int count;
        IntPtr memory = CommandLineToArgvW(text, out count);
        if (memory == IntPtr.Zero) throw new InvalidOperationException("Unable to parse process command line");
        try {
            string[] args = new string[count];
            for (int i = 0; i < count; i++) args[i] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(memory, i * IntPtr.Size));
            return args;
        } finally { LocalFree(memory); }
    }
}
'@
function Is-OwnedCollector($taskCandidate) {
    if (-not $taskCandidate -or $taskCandidate.Name -notmatch '^python(w)?\.exe$' -or -not $taskCandidate.CommandLine) { return $false }
    $taskArguments = [CollectorCommandLine]::Parse([string]$taskCandidate.CommandLine)
    for ($taskIndex = 1; $taskIndex -lt $taskArguments.Length; $taskIndex++) {
        $taskArgument = $taskArguments[$taskIndex]
        if ($taskArgument -eq '--') { $taskIndex++; break }
        if ($taskArgument -in @('-u','-B','-E','-I','-s','-S','-O','-OO','-q','-b','-bb','-P','-R')) { continue }
        if ($taskArgument -in @('-X','-W')) { $taskIndex++; continue }
        if ($taskArgument.StartsWith('-X') -or $taskArgument.StartsWith('-W')) { continue }
        if ($taskArgument.StartsWith('-')) { return $false }
        break
    }
    if ($taskIndex -ge $taskArguments.Length) { return $false }
    $taskScript = $taskArguments[$taskIndex]
    if (-not [IO.Path]::IsPathRooted($taskScript)) { return $false }
    return [IO.Path]::GetFullPath($taskScript).Equals($taskCollector, [StringComparison]::OrdinalIgnoreCase)
}
$taskOwned = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" | Where-Object { Is-OwnedCollector $_ })
$taskHandles = [Collections.Generic.List[object]]::new()
try {
    foreach ($taskCandidate in $taskOwned) {
        $taskProcess = $null
        try {
            try { $taskProcess = [Diagnostics.Process]::GetProcessById([int]$taskCandidate.ProcessId) }
            catch [ArgumentException] { continue } # It exited before a handle could be captured.
            # Keep a native handle open before rechecking identity. A PID cannot be
            # recycled beneath this handle; Kill and HasExited refer to this object.
            try { $null = $taskProcess.Handle }
            catch [InvalidOperationException] { continue } # It exited while opening the handle.
            catch [ComponentModel.Win32Exception] {
                # Only a vanished PID is benign; access denied and other errors
                # must stop the update rather than claim the collector stopped.
                if ($_.Exception.NativeErrorCode -in @(87,1168)) { continue }
                throw
            }
            $taskCurrent = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $taskCandidate.ProcessId)
            if (-not (Is-OwnedCollector $taskCurrent) -or $taskCurrent.CreationDate -ne $taskCandidate.CreationDate) { continue }
            if ($taskProcess.HasExited) { continue }
            $taskHandles.Add([pscustomobject]@{ Process=$taskProcess; Candidate=$taskCandidate })
            $taskProcess = $null # The list owns and disposes this handle.
        } finally {
            if ($null -ne $taskProcess) { $taskProcess.Dispose() }
        }
    }

    # A Windows venv can leave both its launcher and the interpreter running
    # the same owned script. Signal all verified processes before waiting, so
    # the launcher's exit never depends on a child we have not stopped yet.
    foreach ($taskEntry in $taskHandles) {
        try { if (-not $taskEntry.Process.HasExited) { $taskEntry.Process.Kill() } }
        catch { if (-not $taskEntry.Process.HasExited) { throw } }
    }
    # Test execution exit, not Win32_Process entry removal: a retained or stale
    # enumeration row is not proof that the original process is still running.
    # One shared deadline stays below the updater caller's 120-second timeout.
    $taskWait = [Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        $taskRunning = @($taskHandles | Where-Object { -not $_.Process.HasExited })
        if ($taskRunning.Count -eq 0) { break }
        if ($taskWait.Elapsed.TotalSeconds -ge 60) {
            throw ('Owned collectors did not stop within 60 seconds (' + [int]$taskWait.Elapsed.TotalSeconds + 's elapsed): PID ' + (($taskRunning | ForEach-Object { $_.Candidate.ProcessId }) -join ', ') + '.')
        }
        Start-Sleep -Milliseconds 100
    }
    foreach ($taskEntry in $taskHandles) {
        Write-Host ('Owned collector stopped: PID ' + $taskEntry.Candidate.ProcessId + ', created ' + $taskEntry.Candidate.CreationDate + '. Spool and database leases were retained.')
    }
} finally {
    foreach ($taskEntry in $taskHandles) { $taskEntry.Process.Dispose() }
}
Write-Host 'Collector stop complete. Other installations and Python processes were preserved.'
