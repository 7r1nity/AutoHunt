param(
    [string]$MockRoot = "C:\job_agent"
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$agentRoot = Join-Path $projectRoot "agent"
$runName = "browser-" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + [guid]::NewGuid().ToString("N").Substring(0,8)
$runRoot = Join-Path $agentRoot ("test-results\" + $runName)
New-Item -ItemType Directory -Path $runRoot | Out-Null
$previousOptIn = $env:AUTOHUNT_MOCK_E2E
$previousRoot = $env:AUTOHUNT_MOCK_ROOT
try {
    $env:AUTOHUNT_MOCK_E2E = "1"
    $env:AUTOHUNT_MOCK_ROOT = $MockRoot
    Push-Location $agentRoot
    try {
        & ".\.venv\Scripts\python.exe" -m pytest tests/test_mock_browser_e2e.py tests/test_delivery_automation.py tests/test_direct_delivery.py tests/test_browser_session.py tests/test_boss_adapter.py tests/test_mock_site_adapter.py -q --basetemp (Join-Path $runRoot "tmp") --junitxml (Join-Path $runRoot "results.xml")
        $testExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    Write-Host "Browser test evidence: $runRoot"
    exit $testExitCode
} finally {
    $env:AUTOHUNT_MOCK_E2E = $previousOptIn
    $env:AUTOHUNT_MOCK_ROOT = $previousRoot
}
