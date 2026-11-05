# Windows PowerShell 5.1 bootstrap. Does not require Python, Node, admin or PATH edits.
param([switch]$NoPause, [string]$CacheDirectory = '')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$managed = Join-Path $root '.restock'
$step = 'bootstrap'
$code = 1
$guard = $null
$run = $null
$hint = 'Check the network/proxy, cache integrity, directory permissions and available disk space, then retry.'
function Stop-Setup([string]$message) {
    $script:hint = $message
    throw [InvalidOperationException]::new($message)
}
function Assert-LocalPath([string]$path) {
    $full = [IO.Path]::GetFullPath($path)
    if (-not $full.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Setup 'Installer target is outside the project.'
    }
    $probe = $full
    while ($probe -and $probe -ne $root) {
        if (Test-Path -LiteralPath $probe) {
            $item = Get-Item -LiteralPath $probe -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { Stop-Setup 'Installer target contains a link/junction.' }
        }
        $probe = Split-Path -Parent $probe
    }
    return $full
}
function Get-ArchiveHash([string]$path) {
    $stream = [IO.File]::OpenRead($path)
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hash.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $hash.Dispose() }
}
function Save-Record($value, [string]$path) {
    [IO.File]::WriteAllText($path, ($value | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding($false)))
}
try {
    if (-not [Environment]::Is64BitProcess -or -not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
        Stop-Setup 'Requires Windows x64 and 64-bit Windows PowerShell.'
    }
    $null = Assert-LocalPath $managed
    $null = New-Item -ItemType Directory -Path $managed -Force
    try { $guard = [IO.File]::Open((Join-Path $managed 'setup.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { Stop-Setup 'The project is running or another setup is active. Stop this project and retry; stale PID records do not block setup.' }
    $run = Assert-LocalPath (Join-Path $root ('outputs\setup\' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fffffff') + '-' + $PID))
    $null = New-Item -ItemType Directory -Path $run -Force
    $lock = Get-Content -LiteralPath (Join-Path $root 'bootstrap-lock.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $runtime = Get-Content -LiteralPath (Join-Path $root 'runtime-lock.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($lock.schema -ne 1 -or $lock.python.version -ne $runtime.python -or $lock.python.sha256 -notmatch '^[a-f0-9]{64}$') {
        Stop-Setup 'Bootstrap manifest does not match runtime-lock.json.'
    }
    if (-not $lock.python.url.StartsWith('https://github.com/astral-sh/python-build-standalone/releases/download/')) { Stop-Setup 'Unexpected Python distribution source.' }
    if ([IO.Path]::GetFileName($lock.python.filename) -ne $lock.python.filename) { Stop-Setup 'Invalid archive name.' }
    if (-not $CacheDirectory) { $CacheDirectory = Join-Path $managed 'cache' }
    $CacheDirectory = [IO.Path]::GetFullPath($CacheDirectory)
    $null = New-Item -ItemType Directory -Path $CacheDirectory -Force
    $destination = Assert-LocalPath (Join-Path $managed 'python')
    $python = Join-Path $destination 'python.exe'
    $step = 'python-distribution'
    if (-not (Test-Path -LiteralPath $destination)) {
        $tar = Join-Path $env:SystemRoot 'System32\tar.exe'
        if (-not (Test-Path -LiteralPath $tar)) { Stop-Setup 'Windows tar.exe is required (Windows 10/11 or supported Server).' }
        $archive = Join-Path $CacheDirectory $lock.python.filename
        if (-not (Test-Path -LiteralPath $archive)) {
            Write-Host 'Downloading the locked standalone Python...'
            $partial = $archive + '.partial-' + [Guid]::NewGuid().ToString('N')
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $ProgressPreference = 'SilentlyContinue'
            try {
                Invoke-WebRequest -Uri $lock.python.url -OutFile $partial -UseBasicParsing -TimeoutSec 180
                if ((Get-ArchiveHash $partial) -ne $lock.python.sha256) { Stop-Setup 'Python download checksum mismatch.' }
                Move-Item -LiteralPath $partial -Destination $archive
            } finally {
                if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
            }
        }
        if ((Get-ArchiveHash $archive) -ne $lock.python.sha256) {
            Stop-Setup 'Cached Python checksum mismatch. Remove only the named cached archive and retry.'
        }
        $names = & $tar -tf $archive 2>&1
        if ($LASTEXITCODE -ne 0 -or -not $names) { Stop-Setup 'Cannot inspect Python archive.' }
        foreach ($name in $names) {
            if ($name -notmatch '^python/' -or $name -match '(^|/)\.\.(/|$)|:|\\') { Stop-Setup 'Unsafe Python archive member.' }
        }
        $stage = Assert-LocalPath (Join-Path $managed ('stage\python-' + [Guid]::NewGuid().ToString('N')))
        $null = New-Item -ItemType Directory -Path $stage -Force
        & $tar -xf $archive -C $stage
        if ($LASTEXITCODE -ne 0) { Stop-Setup 'Python archive extraction failed.' }
        $candidate = Join-Path $stage 'python'
        $binaryHash = (Get-ArchiveHash (Join-Path $candidate 'python.exe'))
        Save-Record @{archive_sha256=$lock.python.sha256; executable_sha256=$binaryHash} (Join-Path $candidate '.restock-receipt.json')
        $null = Assert-LocalPath $candidate
        $null = Assert-LocalPath $destination
        Move-Item -LiteralPath $candidate -Destination $destination
    }
    $receipt = Get-Content -LiteralPath (Join-Path $destination '.restock-receipt.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($receipt.archive_sha256 -ne $lock.python.sha256 -or (Get-ArchiveHash $python) -ne $receipt.executable_sha256) { Stop-Setup 'Existing Python does not match its verified receipt; it was preserved.' }
    $version = & $python -I -c "import platform; print(platform.python_version() + ' ' + platform.machine())" 2>&1
    if ($LASTEXITCODE -ne 0 -or $version -ne ($runtime.python + ' AMD64')) { Stop-Setup 'Standalone Python version/architecture mismatch.' }
    Save-Record @{status='verified'; version=$runtime.python; archive_sha256=$lock.python.sha256} (Join-Path $run 'bootstrap.json')
    $step = 'environment-install'
    & $python -I (Join-Path $PSScriptRoot 'install_environment.py') --root $root --cache $CacheDirectory --run $run
    $code = $LASTEXITCODE
} catch {
    # No raw exception/headers/environment dumps: diagnostic paths can contain credentials.
    Write-Host ('Installation stopped at ' + $step + '. Existing configuration was preserved.')
    Write-Host $hint
    if ($run) { Save-Record @{status='failed'; stage=$step; hint=$hint; exception=$_.Exception.GetType().FullName; line=$_.InvocationInfo.ScriptLineNumber; error_id=$_.FullyQualifiedErrorId} (Join-Path $run 'bootstrap-error.json') }
    $code = 1
} finally {
    if ($guard) { $guard.Dispose() }
    if ($run) { Write-Host ('Evidence: ' + $run) }
    if (-not $NoPause) { $null = Read-Host 'Press Enter to close' }
}
exit $code
