param(
    [ValidateSet('start', 'stop', 'status', 'prepare')][string]$Action = 'start',
    [switch]$NoBrowser,
    [ValidateRange(1024, 65535)][int]$BackendPort = 8000,
    [ValidateRange(1024, 65535)][int]$FrontendPort = 3000
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot 'agent\.venv\Scripts\python.exe'
try {
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        $candidates = @($env:AUTOHUNT_PYTHON)
        foreach ($name in @('py.exe', 'python.exe')) {
            $command = Get-Command $name -ErrorAction SilentlyContinue
            if ($command) { $candidates += $command.Source }
        }
        $pythonPath = $null
        foreach ($candidate in $candidates) {
            if (-not $candidate -or -not (Test-Path -LiteralPath $candidate)) { continue }
            & $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) { $pythonPath = $candidate; break }
        }
        if (-not $pythonPath) { throw 'Python 3.12+ was not found. Install Python, or set AUTOHUNT_PYTHON to python.exe.' }
    }
    $env:PYTHONUTF8 = '1'
    $launchArgs = @((Join-Path $PSScriptRoot 'local_launcher.py'), $Action,
        '--backend-port', $BackendPort, '--frontend-port', $FrontendPort)
    if ($NoBrowser) { $launchArgs += '--no-browser' }
    & $pythonPath @launchArgs
    exit $LASTEXITCODE
} catch {
    Write-Host ("AutoHunt: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
