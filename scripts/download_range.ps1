<#
.SYNOPSIS
    Downloads daily stock + EOD options snapshots from Massive.com across a date range.
    Supports 1..N API keys via $env:MASSIVE_API_KEYS (comma-separated) or -ApiKeys,
    right/entry-band scoping, and deterministic multi-process sharding.
#>

param(
    [string]$Ticker     = "NVDA",
    [string]$StartDate  = "2026-10-01",
    [string]$EndDate    = "2026-11-12",
    [double]$RangePct   = 0.20,
    [double]$StrikeStep = 5.0,
    [int]$MinDte        = 25,
    [int]$MaxDte        = 60,
    [string]$Cadence    = "monthly",
    [ValidateSet("both", "C", "P")]
    [string]$Right      = "both",
    [double]$DelaySec   = 12.5,
    [string[]]$ApiKeys  = @(),
    [int]$ShardIndex    = 0,
    [int]$ShardCount    = 1,
    [switch]$EntryBandsOnly,
    [switch]$DryRun,
    [switch]$DebugApi
)

$hasEnvKey = [bool]($env:MASSIVE_API_KEYS -or $env:MASSIVE_API_KEY -or $env:POLYGON_API_KEY)
if (-not $ApiKeys.Count -and -not $hasEnvKey) {
    Write-Host "[ERROR] Set `$env:MASSIVE_API_KEYS / `$env:MASSIVE_API_KEY or pass -ApiKeys." -ForegroundColor Red
    exit 1
}

Write-Host "Downloading $Ticker [$StartDate .. $EndDate] | +/-$($RangePct * 100)% | step `$$StrikeStep | cadence=$Cadence | right=$Right | shard=$($ShardIndex + 1)/$ShardCount" -ForegroundColor Cyan

$pyArgs = @(
    "scripts/fetch_massive_options.py",
    "--ticker", $Ticker,
    "--start-date", $StartDate,
    "--end-date", $EndDate,
    "--range-pct", $RangePct,
    "--strike-step", $StrikeStep,
    "--min-dte", $MinDte,
    "--max-dte", $MaxDte,
    "--expiry-cadence", $Cadence,
    "--right", $Right,
    "--delay-sec", $DelaySec,
    "--shard-index", $ShardIndex,
    "--shard-count", $ShardCount
)

if ($ApiKeys.Count -gt 0) {
    $pyArgs += @("--api-keys", ($ApiKeys -join ","))
}
if ($EntryBandsOnly) { $pyArgs += "--entry-bands-only" }
if ($DryRun) { $pyArgs += "--dry-run" }
if ($DebugApi) { $pyArgs += "--debug" }

python @pyArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Script exited with code $LASTEXITCODE." -ForegroundColor Red
    exit $LASTEXITCODE
}
