# Run with Windows PowerShell 7+; uses a fake uvx, never installs dependencies.
param([Parameter(Mandatory = $true)][string] $Launcher)
$ErrorActionPreference = 'Stop'
$Launcher = (Resolve-Path -LiteralPath $Launcher).Path
$root = Join-Path $env:TEMP ('porter-skill-smoke-' + [guid]::NewGuid().ToString('N'))
$null = [IO.Directory]::CreateDirectory($root)

try {
    $state = Join-Path $root '.state'
    $inherited = Join-Path $root 'inherited-cache'
    $env:PORTER_SKILL_STATE_DIR = $state
    $env:UV_CACHE_DIR = $inherited

    & $Launcher --host-check
    if ($LASTEXITCODE -ne 0) { throw "host-check returned $LASTEXITCODE" }
    foreach ($part in @('uv-cache', 'uv-tools', 'uv-python', 'cache', 'porter-cache')) {
        if (-not (Test-Path -LiteralPath (Join-Path $state $part))) { throw "missing state dir: $part" }
    }
    if ($env:UV_CACHE_DIR -ne (Join-Path $state 'uv-cache')) { throw 'uv cache not overridden' }
    if (Test-Path -LiteralPath $inherited) { throw 'inherited cache was used' }

    # A PowerShell function stands in for uvx; capture the arguments and
    # environment seen by the real entry point without a network request.
    $global:calls = @()
    function global:uvx {
        $global:calls += ,@($args)
        $global:cacheAtCall = $env:UV_CACHE_DIR
        $global:registryAtCall = $env:PORTER_CACHE_DIR
        $global:LASTEXITCODE = 0
    }
    $env:PORTER_SKILL_SPEC = 'porter-workflow[llm,asr-local]'
    $source = 'https://example.org/video?x=1&y=2'
    & $Launcher inspect $source --json
    if ($LASTEXITCODE -ne 0) { throw "inspect returned $LASTEXITCODE" }
    if ($global:calls.Count -ne 1) { throw "expected one uvx invocation, got $($global:calls.Count)" }
    $expected = @('--from', $env:PORTER_SKILL_SPEC, 'porter', 'inspect', $source, '--json')
    if (Compare-Object $expected $global:calls[0]) { throw "uvx arguments were not forwarded: $($global:calls[0] -join ' ')" }
    if ($global:cacheAtCall -ne (Join-Path $state 'uv-cache')) { throw 'uvx saw the wrong cache path' }
    if ($global:registryAtCall -ne (Join-Path $state 'porter-cache')) { throw 'uvx saw the wrong job registry path' }

    Remove-Item Env:PORTER_SKILL_SPEC
    & $Launcher jobs list
    if ($LASTEXITCODE -ne 0) { throw "jobs list returned $LASTEXITCODE" }
    $expected = @('--from', 'porter-workflow[llm]', 'porter', 'jobs', 'list')
    if ($global:calls.Count -ne 2 -or (Compare-Object $expected $global:calls[1])) {
        throw 'the default spec or jobs arguments were not forwarded'
    }

    $env:PORTER_SKILL_STATE_DIR = 'relative-state'
    & $Launcher --host-check
    if ($LASTEXITCODE -ne 2) { throw "relative state should fail with 2, got $LASTEXITCODE" }

    $blocked = Join-Path $root 'file-not-directory'
    [IO.File]::WriteAllText($blocked, 'not a directory')
    $env:PORTER_SKILL_STATE_DIR = $blocked
    & $Launcher --host-check
    if ($LASTEXITCODE -ne 1) { throw "blocked state should fail with 1, got $LASTEXITCODE" }

    Write-Output 'PowerShell skill launcher smoke passed'
} finally {
    Remove-Item Function:\uvx -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $root -Recurse -Force
}
