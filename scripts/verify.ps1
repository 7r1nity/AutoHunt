param([switch]$Browser, [string]$MockRoot = "C:\job_agent")

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$runRoot = Join-Path $projectRoot (".runtime\verify-" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + [guid]::NewGuid().ToString("N").Substring(0,8))
New-Item -ItemType Directory -Path $runRoot | Out-Null
$previousOptIn = $env:AUTOHUNT_MOCK_E2E
try {
    # The browser suite is explicitly separate; tests/conftest.py isolates data/keys.
    $env:AUTOHUNT_MOCK_E2E = "0"
    Push-Location (Join-Path $projectRoot "agent")
    try {
        & ".\.venv\Scripts\python.exe" -m pytest tests -q --basetemp (Join-Path $runRoot "tmp") --junitxml (Join-Path $runRoot "unit.xml")
        if ($LASTEXITCODE -ne 0) { throw "Backend regression tests failed ($LASTEXITCODE)." }
    } finally { Pop-Location }

    Push-Location $projectRoot
    try {
        & pnpm build
        if ($LASTEXITCODE -ne 0) { throw "Frontend production build failed ($LASTEXITCODE)." }
        if ($Browser) {
            $shellPath = (Get-Process -Id $PID).Path
            & $shellPath -NoProfile -File (Join-Path $PSScriptRoot "test-browser.ps1") -MockRoot $MockRoot
            if ($LASTEXITCODE -ne 0) { throw "Isolated browser tests failed ($LASTEXITCODE)." }
        }
    } finally { Pop-Location }
    Write-Host "Verification passed. Unit evidence: $runRoot"
    if (-not $Browser) { Write-Host "Browser E2E was not run. Use -Browser for the complete local release gate." }
} finally {
    $env:AUTOHUNT_MOCK_E2E = $previousOptIn
}
