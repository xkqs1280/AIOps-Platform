# AIOps One-click Upgrade Apply Script (PowerShell)
# Executed as a DETACHED process by the backend. Handles:
#   stop -> backup (incl. .env + pg_dump) -> replace (skip .env) -> start -> health check -> done
# Rollback mode: restore from upgrade/backup and restart.
#
# NOTE: This file must be pure ASCII (PowerShell 5.1 reads UTF-8 no-BOM as ANSI and
#       Chinese would be garbled). State messages are written in English.
param(
    [string]$AppRoot = "",
    [string]$Staging = "",
    [string]$FromVersion = "",
    [string]$ToVersion = "",
    [string]$StateFile = "",
    [string]$SkipDbDump = "0",
    [string]$PythonPath = "",
    [switch]$Rollback
)

$ErrorActionPreference = "Continue"

function Write-State {
    param([string]$State, [int]$Progress, [string]$Message, [string]$ErrorMsg = "")
    $path = $StateFile
    if (-not $path) { $path = Join-Path $AppRoot "upgrade\state.json" }
    $dir = Split-Path -Parent $path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    # NOTE: local var must NOT be named $state (PowerShell is case-insensitive, so it
    #       would collide with the [string]$State param, get type-constrained to a
    #       string, and fail on $state.log = ...). Use $stateObj.
    $stateObj = @{
        state = $State
        progress = $Progress
        message = $Message
        error = $ErrorMsg
        log = @()
    }
    if (Test-Path $path) {
        try {
            $old = Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($old -and $old.log) { $stateObj.log = @($old.log) }
            if ($old) {
                $stateObj.from_version = $old.from_version
                $stateObj.to_version = $old.to_version
                $stateObj.started_at = $old.started_at
            }
        } catch { }
    }
    $ts = Get-Date -Format "HH:mm:ss"
    $stateObj.log = @($stateObj.log) + @("[$ts] $Message")
    if ($stateObj.log.Count -gt 200) { $stateObj.log = $stateObj.log | Select-Object -Last 200 }
    $stateObj | ConvertTo-Json -Depth 6 | Out-File -FilePath $path -Encoding UTF8
}

function Find-PgDump {
    $g = Get-Command pg_dump -ErrorAction SilentlyContinue
    if ($g) { return $g.Source }
    foreach ($root in @("HKLM:\SOFTWARE\PostgreSQL\Installs", "HKLM:\SOFTWARE\WOW6432Node\PostgreSQL\Installs")) {
        if (Test-Path $root) {
            foreach ($k in Get-ChildItem $root) {
                try {
                    $base = (Get-ItemProperty $k.PSPath -Name "Base Directory" -ErrorAction Stop)."Base Directory"
                    $cand = Join-Path $base "bin\pg_dump.exe"
                    if (Test-Path $cand) { return $cand }
                } catch { }
            }
        }
    }
    foreach ($v in @("19","18","17","16","15","14","13")) {
        foreach ($base in @("C:\Program Files\PostgreSQL", "C:\PostgreSQL", "D:\PostgreSQL", "D:\Program Files\PostgreSQL")) {
            $cand = "$base\$v\bin\pg_dump.exe"
            if (Test-Path $cand) { return $cand }
        }
    }
    return $null
}

function Read-DbUrl {
    # Read DATABASE_URL from backend/.env (or AppRoot/.env)
    $envFile = Join-Path $AppRoot "backend\.env"
    if (-not (Test-Path $envFile)) { $envFile = Join-Path $AppRoot ".env" }
    if (-not (Test-Path $envFile)) { return $null }
    $content = Get-Content $envFile -Raw
    if ($content -match "(?m)^DATABASE_URL=(.+)$") {
        return $Matches[1].Trim()
    }
    return $null
}

# Source deployment (Windows dev, run via python aiops_entry.py) when backend/app exists
$IsSource = Test-Path (Join-Path $AppRoot "backend\app\main.py")

function Stop-App {
    # Stop the Windows service first (if installed), then kill exe as fallback.
    $svc = Get-Service -Name "AIOpsPlatform" -ErrorAction SilentlyContinue
    if ($svc) {
        try { Stop-Service -Name "AIOpsPlatform" -Force -ErrorAction Stop } catch { }
        Start-Sleep -Seconds 2
    }
    if ($IsSource) {
        # Source deployment: stop the python backend (aiops_entry.py or uvicorn app.main:app)
        Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match "aiops_entry\.py|app\.main:app" } |
            ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch { } }
        Start-Sleep -Seconds 2
    }
    taskkill /f /im AIOpsServer.exe 2>$null | Out-Null
    taskkill /f /im AIOpsService.exe 2>$null | Out-Null
    # Port 8000 must be free before replacing files
    for ($i = 0; $i -lt 15; $i++) {
        $conn = netstat -ano | Select-String ":8000 " | Select-String "LISTENING"
        if (-not $conn) { return }
        Start-Sleep -Seconds 1
    }
}

function Start-App {
    $svc = Get-Service -Name "AIOpsPlatform" -ErrorAction SilentlyContinue
    if ($svc) {
        try { Start-Service -Name "AIOpsPlatform" -ErrorAction Stop } catch { }
        # Give the service a moment; if it fails to start, fall through to vbs/exe launch
        Start-Sleep -Seconds 3
        $svc2 = Get-Service -Name "AIOpsPlatform" -ErrorAction SilentlyContinue
        if ($svc2 -and $svc2.Status -eq "Running") { return }
        Write-Host "Service AIOpsPlatform did not reach Running, falling back to vbs/exe launch"
    }
    if ($IsSource) {
        # Source deployment: start python backend (inherits PYTHONPATH from the parent env)
        $py = $PythonPath
        if (-not $py) {
            $venvPy = Join-Path $AppRoot "backend\.venv\Scripts\python.exe"
            if (Test-Path $venvPy) { $py = $venvPy } else { $py = "python" }
        }
        if (Test-Path (Join-Path $AppRoot "backend\aiops_entry.py")) {
            Start-Process $py -ArgumentList "aiops_entry.py" -WorkingDirectory (Join-Path $AppRoot "backend") -WindowStyle Hidden
            return
        }
    }
    # Hidden launch (same as deploy/start_hidden.vbs logic, but pure PS to avoid VBS)
    $vbs = Join-Path $AppRoot "deploy\start_hidden.vbs"
    if (Test-Path $vbs) {
        Start-Process wscript.exe -ArgumentList "`"$vbs`"" -WindowStyle Hidden
        return
    }
    $exe = Join-Path $AppRoot "AIOpsServer.exe"
    if (Test-Path $exe) {
        Start-Process $exe -WorkingDirectory $AppRoot -WindowStyle Hidden
    }
}

function Wait-Healthy {
    # HTTPS-compatible health check (self-signed).
    # PS 5.1's ServerCertificateValidationCallback scriptblock fails on the .NET thread
    # ("no runspace"), so use curl.exe (built into Win10 1803+) with -k to skip certs.
    #
    # Wait window: up to 150 x (3s sleep + probe) ~= 7+ minutes. The backend exe is a
    # PyInstaller onefile (90MB+), whose cold start (extract to temp + AV scan +
    # uvicorn boot + DB connect) can take 2-5 minutes on slow/AV-protected disks.
    # Shorter windows caused false "health check timeout" after successful replace
    # (the process was still booting and came up minutes later).
    $url = "http://127.0.0.1:8000/health"
    if (Test-Path (Join-Path $AppRoot "backend\certs\server.crt")) {
        $url = "https://127.0.0.1:8000/health"
    }
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 150; $i++) {
        Start-Sleep -Seconds 3
        if ($curl) {
            try {
                $resp = & $curl.Source -sk -m 8 $url 2>$null
                if ($LASTEXITCODE -eq 0 -and "$resp" -match "healthy|ok") { return $true }
            } catch { }
        } else {
            try {
                $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 8 -ErrorAction Stop
                if ($r.StatusCode -eq 200) { return $true }
            } catch { }
        }
        # If the process died (not just slow), fail fast with diagnostics
        $procs = Get-Process -Name "AIOpsServer" -ErrorAction SilentlyContinue
        if (-not $procs) {
            $svc = Get-Service -Name "AIOpsPlatform" -ErrorAction SilentlyContinue
            if (-not $svc) {
                Write-Host "Health probe ${i}: process AIOpsServer is not running"
            }
        }
    }
    return $false
}

function Backup-Existing {
    param([string]$BackupDir)
    New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
    $exe = Join-Path $AppRoot "AIOpsServer.exe"
    if (Test-Path $exe) { Copy-Item $exe (Join-Path $BackupDir "AIOpsServer.exe") -Force }
    $service = Join-Path $AppRoot "AIOpsService.exe"
    if (Test-Path $service) { Copy-Item $service (Join-Path $BackupDir "AIOpsService.exe") -Force }
    $front = Join-Path $AppRoot "frontend\dist"
    if (Test-Path $front) {
        if (Test-Path (Join-Path $BackupDir "frontend")) { Remove-Item -Recurse -Force (Join-Path $BackupDir "frontend") }
        Copy-Item $front (Join-Path $BackupDir "frontend") -Recurse -Force
    }
    $envFile = Join-Path $AppRoot "backend\.env"
    if (Test-Path $envFile) { Copy-Item $envFile (Join-Path $BackupDir ".env") -Force }
    $src = Join-Path $AppRoot "backend\app"
    if ($IsSource -and (Test-Path $src)) {
        if (Test-Path (Join-Path $BackupDir "app")) { Remove-Item -Recurse -Force (Join-Path $BackupDir "app") }
        Copy-Item $src (Join-Path $BackupDir "app") -Recurse -Force
    }
}

function Restore-Backup {
    param([string]$BackupDir)
    $exe = Join-Path $BackupDir "AIOpsServer.exe"
    if (Test-Path $exe) { Copy-Item $exe (Join-Path $AppRoot "AIOpsServer.exe") -Force }
    $service = Join-Path $BackupDir "AIOpsService.exe"
    if (Test-Path $service) { Copy-Item $service (Join-Path $AppRoot "AIOpsService.exe") -Force }
    $front = Join-Path $BackupDir "frontend"
    if (Test-Path $front) {
        if (Test-Path (Join-Path $AppRoot "frontend\dist")) { Remove-Item -Recurse -Force (Join-Path $AppRoot "frontend\dist") }
        New-Item -ItemType Directory -Force -Path (Join-Path $AppRoot "frontend") | Out-Null
        Copy-Item $front (Join-Path $AppRoot "frontend\dist") -Recurse -Force
    }
    $env = Join-Path $BackupDir ".env"
    if (Test-Path $env) { Copy-Item $env (Join-Path $AppRoot "backend\.env") -Force }
    $src = Join-Path $BackupDir "app"
    if ($IsSource -and (Test-Path $src)) {
        if (Test-Path (Join-Path $AppRoot "backend\app")) { Remove-Item -Recurse -Force (Join-Path $AppRoot "backend\app") }
        New-Item -ItemType Directory -Force -Path (Join-Path $AppRoot "backend") | Out-Null
        Copy-Item $src (Join-Path $AppRoot "backend\app") -Recurse -Force
    }
}

function Dump-Database {
    $pgdump = Find-PgDump
    if (-not $pgdump) { return $false }
    $url = Read-DbUrl
    if (-not $url) { return $false }
    # postgresql+psycopg_async://user:pass@host:port/db
    if ($url -notmatch "^postgresql\+psycopg_async://([^:]+):([^@]+)@([^:]+):(\d+)/(\w+)") { return $false }
    $dbUser = $Matches[1]; $dbPass = $Matches[2]; $dbHost = $Matches[3]; $dbPort = $Matches[4]; $dbName = $Matches[5]
    $backupDir = Join-Path $AppRoot "upgrade\backup"
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    $out = Join-Path $backupDir "db_${dbName}_$($(Get-Date -Format 'yyyyMMdd_HHmmss')).sql"
    $env:PGPASSWORD = $dbPass
    try {
        & $pgdump -U $dbUser -h $dbHost -p $dbPort -d $dbName -F c -f $out 2>$null
        if ($LASTEXITCODE -eq 0 -and (Test-Path $out)) { return $true }
    } finally {
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    }
    return $false
}

# ============================================================
if (-not $AppRoot) { $AppRoot = Split-Path -Parent $PSScriptRoot }
if (-not $StateFile) { $StateFile = Join-Path $AppRoot "upgrade\state.json" }
Write-Host "AIOps upgrade/rollback script. AppRoot=$AppRoot"

if ($Rollback) {
    Write-State -State "rolled_back" -Progress 10 -Message "Rollback started"
    $backupDir = Join-Path $AppRoot "upgrade\backup"
    if (-not (Test-Path $backupDir)) {
        Write-State -State "failed" -Progress 0 -Message "No backup available" -ErrorMsg "backup dir missing"
        exit 1
    }
    Stop-App
    Write-State -State "rolled_back" -Progress 40 -Message "Service stopped, restoring backup"
    Restore-Backup -BackupDir $backupDir
    Write-State -State "rolled_back" -Progress 70 -Message "Backup restored, starting service"
    Start-App
    if (Wait-Healthy) {
        Write-State -State "rolled_back" -Progress 100 -Message "Rollback completed, service healthy"
    } else {
        Write-State -State "failed" -Progress 0 -Message "Rollback completed but health check failed" -ErrorMsg "health check timeout"
    }
    exit 0
}

# ---- Upgrade flow ----
Write-State -State "backup" -Progress 20 -Message "Stopping service"
Stop-App

Write-State -State "backup" -Progress 30 -Message "Backing up current files"
$backupDir = Join-Path $AppRoot "upgrade\backup"
Backup-Existing -BackupDir $backupDir

Write-State -State "backup" -Progress 45 -Message "Dumping database snapshot"
$dbDumped = $false
if ($SkipDbDump -ne "1") { $dbDumped = Dump-Database }
if ($dbDumped) { Write-State -State "backup" -Progress 55 -Message "Database snapshot saved" }
else { Write-State -State "backup" -Progress 55 -Message "DB dump skipped (pg_dump not found or disabled)" }

# ---- Replace (preserve backend/.env) ----
Write-State -State "replacing" -Progress 65 -Message "Replacing application files"
$fail = $false
try {
    if ($Staging -and (Test-Path (Join-Path $Staging "AIOpsServer.exe"))) {
        Copy-Item (Join-Path $Staging "AIOpsServer.exe") (Join-Path $AppRoot "AIOpsServer.exe") -Force
    }
    if ($Staging -and (Test-Path (Join-Path $Staging "AIOpsService.exe"))) {
        Copy-Item (Join-Path $Staging "AIOpsService.exe") (Join-Path $AppRoot "AIOpsService.exe") -Force
    }
    if ($Staging -and (Test-Path (Join-Path $Staging "frontend\dist"))) {
        $dst = Join-Path $AppRoot "frontend\dist"
        if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
        New-Item -ItemType Directory -Force -Path (Join-Path $AppRoot "frontend") | Out-Null
        Copy-Item (Join-Path $Staging "frontend\dist") $dst -Recurse -Force
    }
    # Sync upgrade scripts so the deployed copy stays in sync with this version
    if ($Staging -and (Test-Path (Join-Path $Staging "deploy\upgrade_apply.ps1"))) {
        New-Item -ItemType Directory -Force -Path (Join-Path $AppRoot "deploy") | Out-Null
        Copy-Item (Join-Path $Staging "deploy\upgrade_apply.ps1") (Join-Path $AppRoot "deploy\upgrade_apply.ps1") -Force
    }
    # Sync autostart wrapper (kills stale instances, retries ONCE, then gives up)
    # to prevent the AIOpsServer.exe process pile-up after reboot.
    if ($Staging -and (Test-Path (Join-Path $Staging "deploy\autostart.bat"))) {
        New-Item -ItemType Directory -Force -Path (Join-Path $AppRoot "deploy") | Out-Null
        Copy-Item (Join-Path $Staging "deploy\autostart.bat") (Join-Path $AppRoot "deploy\autostart.bat") -Force
    }
    # Source deployment: replace backend/app python source
    if ($IsSource -and $Staging -and (Test-Path (Join-Path $Staging "backend\app"))) {
        $srcDst = Join-Path $AppRoot "backend\app"
        if (Test-Path $srcDst) { Remove-Item -Recurse -Force $srcDst }
        New-Item -ItemType Directory -Force -Path (Join-Path $AppRoot "backend") | Out-Null
        Copy-Item (Join-Path $Staging "backend\app") $srcDst -Recurse -Force
    }
} catch {
    $fail = $true
    $err = $_.Exception.Message
    Write-State -State "failed" -Progress 0 -Message "Replace failed: $err" -ErrorMsg $err
}
if ($fail) {
    # Rollback automatically
    Write-State -State "failed" -Progress 0 -Message "Replace failed, rolling back"
    Restore-Backup -BackupDir $backupDir
    Start-App
    Write-State -State "rolled_back" -Progress 0 -Message "Rolled back after replace failure"
    exit 1
}

Write-State -State "restarting" -Progress 80 -Message "Starting new version"
Start-App

Write-State -State "verifying" -Progress 90 -Message "Waiting for service health"
if (Wait-Healthy) {
    Write-State -State "done" -Progress 100 -Message "Upgrade completed successfully"
    Write-Host "Upgrade done."
    exit 0
} else {
    Write-State -State "failed" -Progress 0 -Message "Health check failed after restart" -ErrorMsg "health check timeout"
    exit 1
}
