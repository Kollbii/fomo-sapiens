<#
.SYNOPSIS
    Downloads daily stock + EOD options snapshots from Massive.com across a date range.
    Uses range-aggregates mode (~12 minutes total for 6 weeks on the 5 req/min Basic plan).
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
    [switch]$DryRun,
    [switch]$DebugApi
)

if (-not $env:MASSIVE_API_KEY -and -not $env:POLYGON_API_KEY) {
    Write-Host "[ERROR] Set `$env:MASSIVE_API_KEY first." -ForegroundColor Red
    exit 1
}

Write-Host "Downloading $Ticker [$StartDate .. $EndDate] | +/-$($RangePct * 100)% | step `$$StrikeStep | cadence=$Cadence" -ForegroundColor Cyan

$pyArgs = @(
    "scripts/fetch_massive_options.py",
    "--ticker", $Ticker,
    "--start-date", $StartDate,
    "--end-date", $EndDate,
    "--range-pct", $RangePct,
    "--strike-step", $StrikeStep,
    "--min-dte", $MinDte,
    "--max-dte", $MaxDte,
    "--expiry-cadence", $Cadence
)

if ($DryRun) { $pyArgs += "--dry-run" }
if ($DebugApi) { $pyArgs += "--debug" }

python @pyArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Script exited with code $LASTEXITCODE." -ForegroundColor Red
    exit $LASTEXITCODE
}
