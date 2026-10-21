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
foreach ($taskCandidate in $taskOwned) {
    $taskCurrent = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $taskCandidate.ProcessId)
    if (-not (Is-OwnedCollector $taskCurrent) -or $taskCurrent.CreationDate -ne $taskCandidate.CreationDate) { continue }
    try { Stop-Process -Id $taskCurrent.ProcessId -Force -ErrorAction Stop }
    catch {
        $taskRemaining = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $taskCandidate.ProcessId)
        if ((Is-OwnedCollector $taskRemaining) -and $taskRemaining.CreationDate -eq $taskCandidate.CreationDate) { throw }
    }
    $taskDeadline = [DateTime]::UtcNow.AddSeconds(15)
    do {
        $taskRemaining = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $taskCandidate.ProcessId)
        if (-not $taskRemaining -or $taskRemaining.CreationDate -ne $taskCandidate.CreationDate) { break }
        if ([DateTime]::UtcNow -ge $taskDeadline) { throw 'Owned collector did not stop within 15 seconds.' }
        Start-Sleep -Milliseconds 100
    } while ($true)
    Write-Host ('Owned collector stopped: PID ' + $taskCandidate.ProcessId + ', created ' + $taskCandidate.CreationDate + '. Spool and database leases were retained.')
}
Write-Host 'Collector stop complete. Other installations and Python processes were preserved.'
