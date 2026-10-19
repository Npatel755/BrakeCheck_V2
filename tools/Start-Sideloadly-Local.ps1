param([switch]$ValidateOnly)

$ErrorActionPreference = 'Stop'
$appBase = Join-Path $env:LOCALAPPDATA 'Sideloadly'
$cache = Join-Path $appBase 'an'
$repairBase = Join-Path $env:LOCALAPPDATA 'BrakeCheck\SideloadlyRepair'
$bundle = Join-Path $repairBase 'anis-64.zip'
$bundleHash = '9217DADE13E082B7621F568D3BA712C44DD633630C20F3682B62656F31E9DC22'

if (-not (Test-Path -LiteralPath (Join-Path $appBase 'sideloadly.exe'))) {
    throw 'Install Sideloadly before using this launcher.'
}
if (-not (Test-Path -LiteralPath $bundle)) {
    New-Item -ItemType Directory -Path $repairBase -Force | Out-Null
    Invoke-WebRequest -Uri 'https://sideloadly.io/anis-64.zip' -OutFile $bundle -UseBasicParsing
}
if ((Get-FileHash -LiteralPath $bundle -Algorithm SHA256).Hash -ne $bundleHash) {
    throw 'The official Anisette bundle changed. Review it before updating this launcher.'
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead($bundle)
$cacheValid = $true
try {
    $entries = @($archive.Entries | Where-Object { $_.Name -like '*.dll' })
    if ($entries.Count -ne 36) { throw 'Unexpected official DLL bundle contents.' }
    foreach ($entry in $entries) {
        $cachedFile = Join-Path $cache $entry.Name
        if (-not (Test-Path -LiteralPath $cachedFile)) { $cacheValid = $false; break }
        $stream = $entry.Open()
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $expectedHash = [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '')
        } finally { $stream.Dispose(); $sha.Dispose() }
        if ((Get-FileHash -LiteralPath $cachedFile -Algorithm SHA256).Hash -ne $expectedHash) {
            $cacheValid = $false
            break
        }
    }
    if (@(Get-ChildItem -LiteralPath $cache -File -Filter '*.dll' -ErrorAction SilentlyContinue).Count -ne 36) {
        $cacheValid = $false
    }
} finally { $archive.Dispose() }

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class BrakeCheckSideloadlyDlls {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern bool SetDllDirectory(string path);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    public static extern IntPtr LoadLibraryEx(string path, IntPtr file, uint flags);
}
'@

if ($ValidateOnly) {
    if (-not $cacheValid) { throw 'The Local Anisette cache differs from the official bundle.' }
    $module = [BrakeCheckSideloadlyDlls]::LoadLibraryEx((Join-Path $cache 'iTunesCore.dll'), [IntPtr]::Zero, 8)
    if ($module -eq [IntPtr]::Zero) {
        throw "DLL loading failed with Windows error $([Runtime.InteropServices.Marshal]::GetLastWin32Error())."
    }
    # CoreFoundation starts workers. Keep its module loaded until process exit.
    Write-Output 'Official bundle checksum, all 36 DLL hashes, and DLL loading: PASS'
    exit 0
}

# Restart both processes; the daemon also retains its inherited DLL search path.
foreach ($name in @('sideloadly', 'sideloadlydaemon')) {
    Get-Process -Name $name -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq (Join-Path $appBase ($name + '.exe')) } |
        Stop-Process
}

if (-not $cacheValid) {
    if (Test-Path -LiteralPath $cache) {
        $resolvedBase = (Resolve-Path -LiteralPath $appBase).Path
        $resolvedCache = (Resolve-Path -LiteralPath $cache).Path
        if ($resolvedCache -ne (Join-Path $resolvedBase 'an')) { throw 'Unexpected cache location.' }
        Rename-Item -LiteralPath $resolvedCache -NewName ('an-before-repair-' + [Guid]::NewGuid().ToString('N'))
    }
    Expand-Archive -LiteralPath $bundle -DestinationPath $cache
}

$originalPath = $env:PATH
$windowsRoot = $env:SystemRoot
$env:PATH = "$cache;$windowsRoot\System32;$windowsRoot;$windowsRoot\System32\Wbem"
try {
    if (-not [BrakeCheckSideloadlyDlls]::SetDllDirectory($cache)) { throw 'Could not set DLL search directory.' }
    Start-Process -FilePath (Join-Path $appBase 'sideloadlydaemon.exe') -WorkingDirectory $cache -WindowStyle Hidden
    Start-Process -FilePath (Join-Path $appBase 'sideloadly.exe') -WorkingDirectory $cache
} finally {
    [BrakeCheckSideloadlyDlls]::SetDllDirectory($null) | Out-Null
    $env:PATH = $originalPath
}
