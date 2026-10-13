# Native Windows entry point for porter-skill. No Bash, WSL or MSYS pipe needed.
# Usage: .\porter.ps1 --host-check | doctor | inspect <URL> --json | run <URL>
$ErrorActionPreference = 'Stop'

$state = $env:PORTER_SKILL_STATE_DIR
if ($state) {
    if (-not [IO.Path]::IsPathRooted($state) -or $state -match '^[A-Za-z]:[^\\/]') {
        [Console]::Error.WriteLine("porter-skill: PORTER_SKILL_STATE_DIR must be absolute: $state")
        exit 2
    }
    # Opt-in sandbox mode: override even pre-existing uv variables that point
    # outside the permitted workspace. Do not guess the workspace from this
    # script's location: skill installation roots differ between agent hosts.
    $env:UV_CACHE_DIR = Join-Path $state 'uv-cache'
    $env:UV_TOOL_DIR = Join-Path $state 'uv-tools'
    $env:UV_TOOL_BIN_DIR = Join-Path $state 'uv-tools/bin'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $state 'uv-python'
    $env:UV_PYTHON_BIN_DIR = Join-Path $state 'uv-python/bin'
    $env:XDG_CACHE_HOME = Join-Path $state 'cache'       # yt-dlp
    $env:PORTER_CACHE_DIR = Join-Path $state 'porter-cache' # job registry (platformdirs ignores XDG on Windows)
}

function Test-DirectoryWritable([string] $dir) {
    try {
        $null = [IO.Directory]::CreateDirectory($dir)
        $probe = Join-Path $dir ('.porter-skill-write-' + [guid]::NewGuid().ToString('N'))
        $stream = [IO.File]::Open($probe, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
        try { $stream.Dispose() } finally { Remove-Item -LiteralPath $probe -Force }
    } catch {
        [Console]::Error.WriteLine("porter-skill: cannot write $dir ($($_.Exception.Message)); choose a writable workspace with PORTER_SKILL_STATE_DIR")
        return $false
    }
    return $true
}

if (-not $state -and $args.Count -gt 0 -and $args[0] -eq '--host-check') {
    [Console]::Error.WriteLine('porter-skill: no workspace state directory configured; default host paths cannot be certified. Set PORTER_SKILL_STATE_DIR to an absolute writable workspace path in a restricted host.')
}
foreach ($name in @('UV_CACHE_DIR', 'UV_TOOL_DIR', 'UV_TOOL_BIN_DIR', 'UV_PYTHON_INSTALL_DIR',
                    'UV_PYTHON_BIN_DIR', 'XDG_CACHE_HOME', 'PORTER_CACHE_DIR')) {
    $dir = [Environment]::GetEnvironmentVariable($name)
    if ($dir -and -not (Test-DirectoryWritable $dir)) { exit 1 }
}

if ($args.Count -gt 0 -and $args[0] -eq '--host-check') {
    [Console]::Error.WriteLine('porter-skill: configured writable directories OK (external commands and network not tested).')
    exit 0
}

$spec = if ($env:PORTER_SKILL_SPEC) { $env:PORTER_SKILL_SPEC } else { 'porter-workflow[llm]' }
try {
    & uvx --from $spec porter @args
    exit $LASTEXITCODE
} catch [System.Management.Automation.CommandNotFoundException] {
    [Console]::Error.WriteLine('porter-skill: uvx not found on PATH; install uv or use a pre-installed porter CLI.')
    exit 127
}
