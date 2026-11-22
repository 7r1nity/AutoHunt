param(
    [ValidateSet('Start', 'Stop', 'Status')][string]$Action = 'Start',
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $projectRoot 'compose.yaml'
$defaultsFile = Join-Path $PSScriptRoot 'compose.defaults.env'

try {
    $dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $dockerCommand) {
        throw 'Docker was not found. Install and start Docker Desktop with Linux containers, then try again.'
    }
    $dockerExecutable = $dockerCommand.Source
    $engineOs = & $dockerExecutable info --format '{{.OSType}}' 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'The Docker Engine is not ready. Start Docker Desktop and wait for the Linux engine.'
    }
    if (($engineOs | Out-String).Trim() -ne 'linux') {
        throw 'AutoHunt requires Linux containers. Switch Docker Desktop to Linux containers.'
    }
    $composeVersion = & $dockerExecutable compose version --short 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker Compose V2 was not found. Update Docker Desktop to include Compose 2.20 or newer.'
    }
    $versionMatch = [regex]::Match(($composeVersion | Out-String).Trim(), '^v?(\d+)\.(\d+)')
    if (-not $versionMatch.Success -or
        ([version]($versionMatch.Groups[1].Value + '.' + $versionMatch.Groups[2].Value)) -lt [version]'2.20') {
        throw 'Docker Compose 2.20 or newer is required. Update Docker Desktop.'
    }

    $composeArgs = @('compose', '--project-name', 'autohunt', '--project-directory', $projectRoot,
        '--env-file', $defaultsFile, '--file', $composeFile)
    if ($Action -eq 'Stop') {
        & $dockerExecutable @composeArgs down --timeout 60
        if ($LASTEXITCODE -ne 0) { throw 'Compose could not stop all services. Inspect docker compose logs.' }
        Write-Host 'AutoHunt containers stopped. Data, login profile and configuration volumes were preserved.'
    } elseif ($Action -eq 'Status') {
        & $dockerExecutable @composeArgs ps
        if ($LASTEXITCODE -ne 0) { throw 'Compose status failed.' }
    } else {
        Write-Host 'Building and starting AutoHunt. The first run downloads images and dependencies.'
        & $dockerExecutable @composeArgs up --build --detach --wait --wait-timeout 240
        if ($LASTEXITCODE -ne 0) {
            throw 'Compose startup failed. Inspect docker compose logs; check whether ports 3000, 6080 or 8080 are already in use. Existing data was preserved.'
        }
        $webUrl = 'http://127.0.0.1:3000'
        $desktopUrl = 'http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale'
        Write-Host ('AutoHunt: ' + $webUrl)
        Write-Host 'Mock recruitment: http://127.0.0.1:8080'
        Write-Host ('Browser login / CAPTCHA / MFA: ' + $desktopUrl)
        Write-Host 'Complete verification in the remote browser, then return to AutoHunt and continue the task.'
        if (-not $NoBrowser) {
            Start-Process -FilePath $webUrl
            Start-Process -FilePath $desktopUrl
        }
    }
} catch {
    Write-Host ('AutoHunt Docker: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
