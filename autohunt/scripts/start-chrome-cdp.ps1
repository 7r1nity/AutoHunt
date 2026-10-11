$ErrorActionPreference = "Stop"

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$profilePath = Join-Path $workspaceRoot "agent\data\private\cdp-chrome-profile"
New-Item -ItemType Directory -Force -Path $profilePath | Out-Null

$candidates = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$chromePath = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $chromePath) {
    throw "Google Chrome was not found. Install Chrome or start it manually with --remote-debugging-port=9223."
}

$arguments = @(
    "--remote-debugging-port=9223",
    "--user-data-dir=$profilePath",
    "--no-first-run",
    "https://www.zhipin.com/"
)
Start-Process -FilePath $chromePath -ArgumentList $arguments
Write-Host "AutoHunt Chrome started on local CDP port 9223. Complete BOSS login, CAPTCHA, or MFA in that window."
