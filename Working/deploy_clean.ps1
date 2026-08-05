# ============================================================================
# DEPRECATED / DEAD SCRIPT — P1 is permanently retired (decision 2026-08-05).
# Retained for reference only. Do not run.
#
# SECURITY 2026-08-05: a hardcoded GitHub PAT was removed from line 2 of this
# file. That token was ALSO XOR-obfuscated (key=11, trivially reversible) inside
# P1/web/admin_dashboard.html, which this script published to the PUBLIC
# GitHub Pages site `stoploss-site` as p1_admin.html. Treat as fully compromised.
# See docs/CREDENTIAL_INCIDENT.md.
# ============================================================================
$ErrorActionPreference = "Stop"
$token = $env:STOPLOSS_DEPLOY_TOKEN
if (-not $token) {
    Write-Error "DEPRECATED SCRIPT. P1 is retired and this deploy path is decommissioned. Refusing to run."
    exit 1
}
$api   = "https://api.github.com/repos/Wave-rock-investments/stoploss-site/contents/p1_admin.html"
$src   = "C:\Users\trish\OneDrive\Desktop\StoplossApk-mt5\P1\web\admin_dashboard.html"
$msg   = "Deploy P1 admin dashboard: refresh admin write token"
Write-Host "Reading $src ..."
$bytes = [System.IO.File]::ReadAllBytes($src)
$b64   = [System.Convert]::ToBase64String($bytes)
Write-Host "  Bytes: $($bytes.Length)  B64 chars: $($b64.Length)"
$hdrs = @{
    Authorization = "token $token"
    "User-Agent"  = "StopLoss-Deploy/1"
    Accept        = "application/vnd.github.v3+json"
}
Write-Host "1. Getting current SHA..."
$existing = $null
try {
    $existing = Invoke-RestMethod -Uri $api -Headers $hdrs -Method GET
    Write-Host "   SHA: $($existing.sha.Substring(0,10))..."
} catch {
    Write-Host "   Not found, will create: $($_.Exception.Message)"
}
$body = @{ message = $msg; content = $b64 }
if ($existing -and $existing.sha) { $body["sha"] = $existing.sha }
$bodyJson = $body | ConvertTo-Json -Compress
Write-Host "2. Uploading..."
try {
    $result = Invoke-RestMethod -Uri $api -Headers $hdrs -Method PUT -Body $bodyJson -ContentType "application/json"
    Write-Host ""
    Write-Host "SUCCESS: p1_admin.html deployed!" -ForegroundColor Green
    Write-Host "  New SHA: $($result.content.sha)"
    Write-Host "  URL: https://wave-rock-investments.github.io/stoploss-site/p1_admin.html"
} catch {
    Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
}
