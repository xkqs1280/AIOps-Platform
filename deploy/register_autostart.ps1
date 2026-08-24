# AIOps autostart registration (startup task + PostgreSQL auto-start)
# Run register_autostart.bat as Administrator.
# Uses schtasks.exe (CLI) for registration because Register-ScheduledTask
# can fail with "RPC server unavailable" on some Windows Server installs.

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "[ERROR] Please run as Administrator." -ForegroundColor Red
    exit 1
}

$batPath = Join-Path $PSScriptRoot "autostart.bat"
if (-not (Test-Path $batPath)) {
    Write-Host "[ERROR] deploy\autostart.bat not found." -ForegroundColor Red
    exit 1
}

Write-Host "Registering AIOpsBackend scheduled task (startup trigger) ..." -ForegroundColor Cyan
$tr = '"' + $batPath + '"'
& schtasks.exe /create /tn "AIOpsBackend" /tr $tr /sc onstart /ru SYSTEM /f
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] schtasks registration failed (code $LASTEXITCODE). Run as Administrator?" -ForegroundColor Red
    exit 1
}
Write-Host "[OK] Scheduled task AIOpsBackend registered." -ForegroundColor Green

# Optional: attach failure-retry settings via PowerShell (non-fatal if it fails)
try {
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Set-ScheduledTask -TaskName "AIOpsBackend" -Settings $settings | Out-Null
    Write-Host "[OK] Retry-on-failure enabled (3x, 1-min interval)." -ForegroundColor Green
} catch {
    Write-Host "[!] Could not attach retry settings (base autostart still works): $_" -ForegroundColor Yellow
}

Write-Host "Setting PostgreSQL services to auto-start ..." -ForegroundColor Cyan
foreach ($v in 18, 17, 16, 15, 14, 13, 12) {
    sc.exe config "postgresql-x64-$v" start= auto 2>$null | Out-Null
}
Write-Host "[OK] PostgreSQL services set to auto-start." -ForegroundColor Green
Write-Host ""
Write-Host "  View:   schtasks /query /tn AIOpsBackend /v /fo LIST"
Write-Host "  Test:   schtasks /run /tn AIOpsBackend"
Write-Host "  Remove: schtasks /delete /tn AIOpsBackend /f"
Write-Host ""
Write-Host "The autostart script itself waits for PostgreSQL, verifies port 8000 and retries,"
Write-Host "so the platform recovers automatically even if the task has no retry settings."
