$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $projectRoot "agent")
try {
    & ".\.venv\Scripts\python.exe" -m compileall -q app
    if ($LASTEXITCODE -ne 0) { throw "Backend syntax check failed ($LASTEXITCODE)." }
} finally { Pop-Location }

Push-Location $projectRoot
try {
    & pnpm typecheck
    if ($LASTEXITCODE -ne 0) { throw "Frontend type check failed ($LASTEXITCODE)." }
    & pnpm build
    if ($LASTEXITCODE -ne 0) { throw "Frontend production build failed ($LASTEXITCODE)." }
} finally { Pop-Location }
Write-Host "Syntax, type and build checks passed. Run agent/evaluation separately for behavioral contracts; see docs/evaluation.md."
