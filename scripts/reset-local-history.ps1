[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$dataRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'agent\data'))
$backupRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot ('.runlogs\history-reset-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))))
# Deliberately not a generic directory cleaner. Stop the backend first.
if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Stop the AutoHunt backend on port 8000 before resetting history.'
}
$configuredRoot = Get-Content -LiteralPath (Join-Path $projectRoot 'agent\.env') |
    Where-Object { $_ -match '^AUTOHUNT_DATA_ROOT=' }
if ($configuredRoot) { throw 'Custom AUTOHUNT_DATA_ROOT requires manual inspection; no data was moved.' }
if ($env:AUTOHUNT_DATA_ROOT) { throw 'Custom process AUTOHUNT_DATA_ROOT requires manual inspection.' }
if (Test-Path -LiteralPath $backupRoot) { throw 'Backup directory already exists.' }

$relativeTargets = @(
    'autohunt.db', 'autohunt.db-wal', 'autohunt.db-shm', 'autohunt.db-journal',
    'runtime.db', 'runtime.db-wal', 'runtime.db-shm', 'runtime.db-journal',
    'memory.md', 'audit', 'cache-backups', 'skill-history', 'skill-proposals',
    'private\resumes', 'private\materials', 'private\candidate-state',
    'private\resume-workspace', 'private\profile.json.enc',
    'private\browser-evidence', 'private\browser-lifecycle.jsonl',
    'private\private\browser-lifecycle.jsonl'
)
$targets = foreach ($relative in $relativeTargets) {
    $target = [IO.Path]::GetFullPath((Join-Path $dataRoot $relative))
    if (-not $target.StartsWith($dataRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Out-of-scope path: $relative"
    }
    if (Test-Path -LiteralPath $target) {
        $item = Get-Item -LiteralPath $target -Force
        $ancestors = @((Get-Item -LiteralPath $dataRoot -Force), (Get-Item -LiteralPath (Split-Path -Parent $target) -Force))
        $children = if ($item.PSIsContainer) { @(Get-ChildItem -LiteralPath $target -Recurse -Force) } else { @() }
        if (@(@($item) + $ancestors + $children | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) {
            throw "Refusing to move a reparse point: $relative"
        }
        [pscustomobject]@{ Relative = $relative; Path = $target }
    }
}
$routerPath = Join-Path $dataRoot 'router.db'
if ($PSCmdlet.ShouldProcess($dataRoot, 'Archive all AutoHunt business history; retain credentials, browser login and rules')) {
    New-Item -ItemType Directory -Path $backupRoot | Out-Null
    foreach ($target in $targets) {
        $destination = Join-Path $backupRoot $target.Relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Move-Item -LiteralPath $target.Path -Destination $destination
    }
    # Router history shares a database with rule configuration; clear only
    # telemetry/clarifications, not the active rule version or its payloads.
    if (Test-Path -LiteralPath $routerPath) {
        foreach ($suffix in @('', '-wal', '-shm')) {
            $source = $routerPath + $suffix
            if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination (Join-Path $backupRoot ('router.db' + $suffix)) }
        }
        & (Join-Path $projectRoot 'agent\.venv\Scripts\python.exe') -c 'import sqlite3, sys; db=sqlite3.connect(sys.argv[1]); tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type=?", ("table",))}; [db.execute("DELETE FROM "+t) for t in ("router_events", "rule_hit_stats", "router_disagreements", "clarify_memory") if t in tables]; db.commit(); db.execute("PRAGMA wal_checkpoint(TRUNCATE)"); db.execute("VACUUM"); db.close()' $routerPath
        if ($LASTEXITCODE -ne 0) { throw 'Router cleanup failed; backup is retained.' }
    }
    [pscustomobject]@{ ArchivedPaths = @($targets).Count; Backup = $backupRoot; ConfigPreserved = $true }
}
