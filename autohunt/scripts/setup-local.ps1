param([switch]$Force)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$frontendEnv = Join-Path $projectRoot "frontend\.env.local"
$backendEnv = Join-Path $projectRoot "agent\.env"

if (-not $Force -and ((Test-Path $frontendEnv) -or (Test-Path $backendEnv))) {
    throw "Local env files already exist. Use -Force only if you intend to replace both files."
}

$tokenBytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
$dataBytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
$apiToken = [Convert]::ToBase64String($tokenBytes)
$dataKey = [Convert]::ToBase64String($dataBytes).Replace("+", "-").Replace("/", "_")

$frontend = @"
AUTOHUNT_BACKEND_URL=http://127.0.0.1:8000
AUTOHUNT_BACKEND_TOKEN=$apiToken
"@

$backend = @"
AUTOHUNT_API_KEY=$apiToken
AUTOHUNT_DATA_KEY=$dataKey
AUTOHUNT_ALLOWED_ORIGINS=http://127.0.0.1:3000,http://localhost:3000
AUTOHUNT_LLM_PROVIDER=mock
AUTOHUNT_LLM_MODEL=mock-autohunt-v1
"@

Set-Content -LiteralPath $frontendEnv -Value $frontend -Encoding utf8NoBOM
Set-Content -LiteralPath $backendEnv -Value $backend -Encoding utf8NoBOM
Write-Host "Created frontend/.env.local and agent/.env with matching random local credentials."
