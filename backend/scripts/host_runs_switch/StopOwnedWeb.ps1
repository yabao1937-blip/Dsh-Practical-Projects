param([Parameter(Mandatory=$true)][string]$InstallDir)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
if (-not (Test-Path -LiteralPath (Join-Path $taskRoot 'offline-install.json'))) { throw 'Not a recognized installation.' }
$taskWeb = [IO.Path]::GetFullPath((Join-Path $taskRoot 'backend\serve.py'))
Add-Type -TypeDefinition @'
using System;
using System.Diagnostics;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class OwnedWebIdentity {
    [DllImport("shell32.dll", SetLastError=true)]
    private static extern IntPtr CommandLineToArgvW([MarshalAs(UnmanagedType.LPWStr)] string text, out int count);
    [DllImport("kernel32.dll")] private static extern IntPtr LocalFree(IntPtr memory);
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern bool ReadProcessMemory(IntPtr process, IntPtr address, byte[] bytes, IntPtr size, out IntPtr count);
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern bool IsWow64Process(IntPtr process, out bool wow64);
    [DllImport("ntdll.dll")]
    private static extern int NtQueryInformationProcess(IntPtr process, int kind, byte[] data, int size, out int returned);
    private static byte[] Read(IntPtr process, long address, int size) {
        byte[] bytes = new byte[size]; IntPtr count;
        if (address <= 0 || !ReadProcessMemory(process, new IntPtr(address), bytes, new IntPtr(size), out count) || count.ToInt64() != size)
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Cannot prove Web working directory");
        return bytes;
    }
    private static long Pointer(byte[] data, int offset, int width) {
        return width == 8 ? BitConverter.ToInt64(data, offset) : (long)BitConverter.ToUInt32(data, offset);
    }
    public static string[] Parse(string text) {
        int count; IntPtr memory = CommandLineToArgvW(text, out count);
        if (memory == IntPtr.Zero) throw new InvalidOperationException("Unable to parse Web command line");
        try {
            string[] args = new string[count];
            for (int i=0; i<count; i++) args[i]=Marshal.PtrToStringUni(Marshal.ReadIntPtr(memory, i*IntPtr.Size));
            return args;
        } finally { LocalFree(memory); }
    }
    public static string Directory(Process process) {
        // This reads only the bounded CWD string. No process memory is modified.
        IntPtr handle = process.Handle;
        bool wow64;
        if (!IsWow64Process(handle, out wow64)) throw new Win32Exception(Marshal.GetLastWin32Error());
        if (IntPtr.Size == 4 && Environment.Is64BitOperatingSystem && !wow64)
            throw new InvalidOperationException("Use 64-bit PowerShell to prove Web ownership");
        int width = wow64 ? 4 : IntPtr.Size;
        byte[] info = new byte[IntPtr.Size * 6]; int returned;
        if (NtQueryInformationProcess(handle, 0, info, info.Length, out returned) != 0)
            throw new InvalidOperationException("Cannot read Web process identity");
        long peb = Pointer(info, IntPtr.Size, IntPtr.Size);
        if (wow64 && IntPtr.Size == 8) {
            byte[] wow = new byte[8];
            if (NtQueryInformationProcess(handle, 26, wow, wow.Length, out returned) != 0)
                throw new InvalidOperationException("Cannot read 32-bit Web identity");
            peb = BitConverter.ToInt64(wow, 0);
        }
        long parameters = Pointer(Read(handle, peb + (width == 8 ? 0x20 : 0x10), width), 0, width);
        byte[] directory = Read(handle, parameters + (width == 8 ? 0x38 : 0x24), width == 8 ? 16 : 8);
        int length = BitConverter.ToUInt16(directory, 0);
        if (length <= 0 || length > 32766 || (length & 1) != 0)
            throw new InvalidOperationException("Invalid Web working directory");
        long buffer = Pointer(directory, width == 8 ? 8 : 4, width);
        return System.Text.Encoding.Unicode.GetString(Read(handle, buffer, length));
    }
}
'@
function Get-WebArgument($taskCandidate) {
    if (-not $taskCandidate -or $taskCandidate.Name -notmatch '^python(w)?\.exe$' -or -not $taskCandidate.CommandLine) { return $null }
    $taskArguments = [OwnedWebIdentity]::Parse([string]$taskCandidate.CommandLine)
    for ($taskIndex=1; $taskIndex -lt $taskArguments.Length; $taskIndex++) {
        $taskArgument = $taskArguments[$taskIndex]
        if ($taskArgument -eq '--') { $taskIndex++; break }
        if ($taskArgument -in @('-u','-B','-E','-I','-s','-S','-O','-OO','-q','-b','-bb','-P','-R')) { continue }
        if ($taskArgument -in @('-X','-W')) { $taskIndex++; continue }
        if ($taskArgument.StartsWith('-X') -or $taskArgument.StartsWith('-W')) { continue }
        if ($taskArgument.StartsWith('-')) { return $null }
        break
    }
    if ($taskIndex -ge $taskArguments.Length) { return $null }
    return $taskArguments[$taskIndex]
}
function Get-WebScript($taskCandidate, $taskProcess) {
    $taskScript = Get-WebArgument $taskCandidate
    if (-not $taskScript) { return $null }
    if (-not [IO.Path]::IsPathRooted($taskScript)) {
        if ([IO.Path]::GetFileName($taskScript) -ine 'serve.py') { return $null }
        $taskScript = Join-Path ([OwnedWebIdentity]::Directory($taskProcess)) $taskScript
    }
    return [IO.Path]::GetFullPath($taskScript)
}
$taskListeners = @(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)
$taskHandles = [Collections.Generic.List[object]]::new()
try {
    foreach ($taskCandidate in @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'")) {
        $taskArgument = Get-WebArgument $taskCandidate
        if (-not $taskArgument) { continue }
        if ([IO.Path]::IsPathRooted($taskArgument)) {
            if (-not [IO.Path]::GetFullPath($taskArgument).Equals($taskWeb,[StringComparison]::OrdinalIgnoreCase)) { continue }
        } elseif ([IO.Path]::GetFileName($taskArgument) -ine 'serve.py') { continue }
        $taskProcess = $null
        try {
            try { $taskProcess = [Diagnostics.Process]::GetProcessById([int]$taskCandidate.ProcessId); $null = $taskProcess.Handle }
            catch [ArgumentException] { continue }
            catch [InvalidOperationException] { continue }
            catch [ComponentModel.Win32Exception] {
                if ($_.Exception.NativeErrorCode -in @(87,1168)) { continue }
                throw
            }
            $taskCurrent = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $taskCandidate.ProcessId)
            if (-not $taskCurrent -or $taskCurrent.CreationDate -ne $taskCandidate.CreationDate -or $taskProcess.HasExited) { continue }
            $taskScript = Get-WebScript $taskCurrent $taskProcess
            if (-not $taskScript -or -not $taskScript.Equals($taskWeb,[StringComparison]::OrdinalIgnoreCase)) { continue }
            $taskHandles.Add([pscustomobject]@{Process=$taskProcess; Candidate=$taskCandidate})
            $taskProcess = $null
        } finally {
            if ($null -ne $taskProcess) { $taskProcess.Dispose() }
        }
    }
    # Prove a listener belongs to one of the captured processes before stopping any.
    foreach ($taskListener in $taskListeners) {
        if ($taskListener.OwningProcess -notin @($taskHandles | ForEach-Object { $_.Candidate.ProcessId })) {
            throw 'Port 8000 belongs to another or unproven process; nothing was stopped.'
        }
    }
    foreach ($taskEntry in $taskHandles) {
        $taskCurrent = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $taskEntry.Candidate.ProcessId)
        if (-not $taskCurrent -or $taskCurrent.CreationDate -ne $taskEntry.Candidate.CreationDate -or
            (Get-WebScript $taskCurrent $taskEntry.Process) -ine $taskWeb) { throw 'Web identity changed; no further process was stopped.' }
    }
    foreach ($taskEntry in $taskHandles) {
        try { if (-not $taskEntry.Process.HasExited) { $taskEntry.Process.Kill() } }
        catch { if (-not $taskEntry.Process.HasExited) { throw } }
    }
    $taskWait = [Diagnostics.Stopwatch]::StartNew()
    while (@($taskHandles | Where-Object { -not $_.Process.HasExited }).Count) {
        if ($taskWait.Elapsed.TotalSeconds -ge 60) { throw 'Owned Web processes did not stop within 60 seconds.' }
        Start-Sleep -Milliseconds 100
    }
} finally {
    foreach ($taskEntry in $taskHandles) { $taskEntry.Process.Dispose() }
}
Write-Host 'Owned Web stop complete. Collector and other installations were preserved.'
