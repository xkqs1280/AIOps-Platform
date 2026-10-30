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

# Source deployment detection: BOTH backend\app\main.py AND backend\aiops_entry.py must exist.
# NOTE (2026-10-08): do NOT test main.py alone. The upgrade package ships backend/app/**
#       (which contains main.py) but NOT backend\aiops_entry.py. An exe install that has
#       upgraded once would therefore start looking like a source install, and Start-App
#       would try to run "aiops_entry.py" with $PythonPath (== AIOpsServer.exe in an exe
#       install) as the interpreter - which can never work.
$IsSource = (Test-Path (Join-Path $AppRoot "backend\app\main.py")) -and `
            (Test-Path (Join-Path $AppRoot "backend\aiops_entry.py"))

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
    # Port 8000 must be free before replacing files. The exe is ~95MB; give the OS time
    # to release the listener and any lingering file handles.
    for ($i = 0; $i -lt 30; $i++) {
        $conn = netstat -ano | Select-String ":8000 " | Select-String "LISTENING"
        if (-not $conn) {
            Write-Host ("[{0}] Stop-App: port 8000 released after ~{1}s" -f (Get-Date -Format "HH:mm:ss"), $i)
            return
        }
        Start-Sleep -Seconds 1
    }
    Write-Host ("[{0}] Stop-App: WARNING port 8000 still LISTENING after 30s" -f (Get-Date -Format "HH:mm:ss"))
}

function Start-App {
    $ts = Get-Date -Format "HH:mm:ss"
    # --- 0) Windows service (only if actually installed) ---
    $svc = Get-Service -Name "AIOpsPlatform" -ErrorAction SilentlyContinue
    if ($svc) {
        Write-Host "[$ts] Start-App: starting Windows service AIOpsPlatform"
        try { Start-Service -Name "AIOpsPlatform" -ErrorAction Stop } catch {
            Write-Host "[$ts] Start-App: Start-Service failed: $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 3
        $svc2 = Get-Service -Name "AIOpsPlatform" -ErrorAction SilentlyContinue
        if ($svc2 -and $svc2.Status -eq "Running") {
            Write-Host "[$ts] Start-App: service AIOpsPlatform is Running"
            return
        }
        Write-Host "[$ts] Service AIOpsPlatform did not reach Running, falling back to vbs/exe launch"
    }

    # --- 1) Wait for PostgreSQL (same guard the normal boot path uses) ---
    # Without this the backend can start before PG is reachable and come up degraded.
    for ($p = 1; $p -le 18; $p++) {
        $pgOk = $false
        try {
            $tc = New-Object Net.Sockets.TcpClient
            $iar = $tc.BeginConnect("127.0.0.1", 5432, $null, $null)
            if ($iar.AsyncWaitHandle.WaitOne(1500)) { $pgOk = $tc.Connected }
            $tc.Close()
        } catch { $pgOk = $false }
        if ($pgOk) { break }
        Start-Sleep -Seconds 5
    }

    # --- 2) Source deployment: launch the python backend ---
    if ($IsSource) {
        $py = $PythonPath
        # Guard: never use a non-python binary as the interpreter. In an exe install the
        # backend passes sys.executable == AIOpsServer.exe, which cannot run .py files.
        if ($py -and ($py -notmatch "python")) {
            Write-Host "[$ts] Start-App: ignoring PythonPath '$py' (not a python interpreter)"
            $py = ""
        }
        $venvPy = Join-Path $AppRoot "backend\.venv\Scripts\python.exe"
        if (-not $py -and (Test-Path $venvPy)) { $py = $venvPy }
        if (-not $py) { $py = "python" }
        $entry = Join-Path $AppRoot "backend\aiops_entry.py"
        if (Test-Path $entry) {
            Write-Host "[$ts] Start-App: launching source backend: $py aiops_entry.py"
            Start-Process $py -ArgumentList "aiops_entry.py" -WorkingDirectory (Join-Path $AppRoot "backend") -WindowStyle Hidden
            return
        }
    }

    # --- 3) Hidden launch via VBS (preferred: fully detached) ---
    $vbs = Join-Path $AppRoot "deploy\start_hidden.vbs"
    if (Test-Path $vbs) {
        Write-Host "[$ts] Start-App: launching via wscript start_hidden.vbs"
        $p = Start-Detached -FilePath "$env:SystemRoot\System32\wscript.exe" -Arguments "`"$vbs`""
        Write-Host "[$ts] Start-App: wscript pid=$($p.Id)"
        return
    }

    # --- 4) Last resort: run the exe directly ---
    $exe = Join-Path $AppRoot "AIOpsServer.exe"
    if (Test-Path $exe) {
        Write-Host "[$ts] Start-App: launching exe directly"
        $p = Start-Detached -FilePath $exe -Arguments ""
        Write-Host "[$ts] Start-App: AIOpsServer pid=$($p.Id)"
    } else {
        Write-Host "[$ts] Start-App: ERROR no launcher found (no service, no vbs, no exe)"
    }
}

function Start-Detached {
    # Launch the app fully detached, with a sanitised environment.
    #
    # Field report 2026-10-30 (upgrade 4.5.8 -> 4.6.1 on one customer box): the script
    # launched the new exe, the process was visible for ~10s and then vanished, while a
    # manual start.bat a few minutes later brought the SAME exe up normally. Two causes
    # are handled here - both cheap, both no-ops when they do not apply:
    #
    #  a) %TEMP%. AIOpsServer.exe is a PyInstaller **onefile** build: every start unpacks
    #     ~250 MB into %TEMP%. This script is spawned by the backend, so it can inherit a
    #     %TEMP% that is unwritable / quota-limited / scanned by AV on every write (e.g.
    #     C:\Windows\Temp under a service account). Pin TEMP/TMP to <AppRoot>\upgrade\tmp
    #     so the unpack lands on the same volume as the app, outside any roaming profile.
    #
    #  b) Inherited frozen-app state. A PyInstaller onefile child process re-uses its
    #     parent's extraction dir instead of unpacking its own (that is what
    #     _PYI_PARENT_PROCESS_LEVEL / _PYI_APPLICATION_HOME_DIR / _MEIPASS are for). The
    #     backend is itself frozen, so a NEW build launched from the OLD process tree can
    #     end up running against the PREVIOUS version's extracted files. Strip those vars
    #     and force PYINSTALLER_RESET_ENVIRONMENT=1 for the replacement process.
    #
    # NOTE: the env inheritance theory was tested on 2026-10-30 by injecting
    # _PYI_PARENT_PROCESS_LEVEL=1 + a bogus _PYI_APPLICATION_HOME_DIR into the 4.6.1 exe -
    # the bootloader ignored them and started normally, so this is hardening, not a proven
    # root cause. The startup log (backend\logs\startup.log) will settle it next time.
    param([string]$FilePath, [string]$Arguments)

    $ts = Get-Date -Format "HH:mm:ss"
    $tmpDir = Join-Path $AppRoot "upgrade\tmp"
    try { New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null } catch { }

    $oldTemp = $env:TEMP
    $oldTmp = $env:TMP
    $env:TEMP = $tmpDir
    $env:TMP = $tmpDir

    # Strip the frozen-app vars by NAME, never by enumerating Env: - enumerating blows up
    # with "an item with the same key has already been added" on hosts whose environment
    # block contains duplicate-case entries such as HTTPS_PROXY + https_proxy (seen on the
    # 2026-10-30 test box). The list below is exactly what the AIOpsServer.exe bootloader
    # reads (verified by scanning the binary for its own strings).
    $stripped = @()
    foreach ($n in @("_PYI_PARENT_PROCESS_LEVEL", "_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE", "_PYI_SPLASH_IPC", "_MEIPASS", "_MEIPASS2")) {
        try {
            if (Test-Path "Env:$n") { Remove-Item "Env:$n" -ErrorAction Stop; $stripped += $n }
        } catch { }
    }
    $env:PYINSTALLER_RESET_ENVIRONMENT = "1"
    $strippedText = "(none)"
    if ($stripped.Count -gt 0) { $strippedText = ($stripped -join ",") }
    Write-Host ("[{0}] Start-App: TEMP -> {1}; stripped inherited frozen-app vars: {2}" -f $ts, $tmpDir, $strippedText)

    if ($Arguments) {
        $p = Start-Process -FilePath $FilePath -ArgumentList $Arguments -WorkingDirectory $AppRoot -WindowStyle Hidden -PassThru
    } else {
        $p = Start-Process -FilePath $FilePath -WorkingDirectory $AppRoot -WindowStyle Hidden -PassThru
    }

    # Restore, so the rest of the script keeps its normal TEMP (the child already copied it).
    $env:TEMP = $oldTemp
    $env:TMP = $oldTmp
    return $p
}

function Test-Port8000 {
    $line = netstat -ano | Select-String ":8000 " | Select-String "LISTENING"
    return [bool]$line
}

function Get-StartupLogTail {
    # Backend writes its own startup log (stdout + traceback) to backend\logs\startup.log
    # since 4.6.2 - see backend\aiops_entry.py. On a hidden launch this is the ONLY place
    # a startup crash is recorded, so surface its tail in state.json (=> visible on the
    # upgrade page) instead of making the operator guess.
    param([int]$Lines = 6)
    $p = Join-Path $AppRoot "backend\logs\startup.log"
    if (-not (Test-Path $p)) { return "" }
    try {
        $tail = @(Get-Content $p -Tail $Lines -ErrorAction Stop)
        $txt = ($tail | ForEach-Object { ("$(_)".Trim()) } | Where-Object { $_ }) -join " | "
        if ($txt.Length -gt 700) { $txt = $txt.Substring($txt.Length - 700) }
        return $txt
    } catch { return "" }
}

function Wait-Healthy {
    # HTTPS-compatible health check (self-signed).
    # PS 5.1's ServerCertificateValidationCallback scriptblock fails on the .NET thread
    # ("no runspace"), so use curl.exe (built into Win10 1803+) with -k to skip certs.
    #
    # IMPORTANT (2026-10-08, production 4.5.7 -> 4.5.9):
    #   The old implementation only probed /health and gave up after a fixed number of
    #   probes. A *successful replace* was then reported as "health check failed" because
    #   the new backend had not started listening yet during the probe window. The upgrade
    #   had actually succeeded - it was a false negative, and the page showed a bogus
    #   "upgrade failed" while the new version was already running.
    #   New behaviour:
    #     * each probe checks (process alive?) + (port 8000 LISTENING?) + (/health ok?)
    #     * while the backend PROCESS is alive we keep waiting (up to the ceiling),
    #       no matter how long the port takes to open;
    #     * we only fail fast when the process has been gone for several consecutive probes;
    #     * every probe writes one diagnostic line so a future incident needs no guessing.
    $url = "http://127.0.0.1:8000/health"
    if (Test-Path (Join-Path $AppRoot "backend\certs\server.crt")) {
        $url = "https://127.0.0.1:8000/health"
    }
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl) {
        Write-Host ("[{0}] Wait-Healthy: WARNING curl.exe not found; using TCP-connect check only" -f (Get-Date -Format "HH:mm:ss"))
    }
    # Ceiling while the process keeps running (slow cold start + DB migration on big data).
    $maxWaitSeconds = 1800
    $probeInterval = 3
    $maxProbes = [int]($maxWaitSeconds / $probeInterval)
    $deadline = (Get-Date).AddSeconds($maxWaitSeconds)
    $goneStreak = 0
    for ($i = 1; $i -le $maxProbes; $i++) {
        Start-Sleep -Seconds $probeInterval
        $procAlive = [bool](Get-Process -Name "AIOpsServer" -ErrorAction SilentlyContinue)
        $listening = Test-Port8000
        $healthy = $false
        if ($listening) {
            if ($curl) {
                try {
                    $resp = & $curl.Source -sk -m 8 $url 2>$null
                    if ($LASTEXITCODE -eq 0 -and "$resp" -match "healthy|ok") { $healthy = $true }
                } catch { }
            } else {
                # No curl available: a listening port is the strongest signal obtainable here.
                $healthy = $true
            }
        }
        Write-Host ("[{0}] Wait-Healthy probe {1}: proc={2} listening={3} healthy={4}" -f (Get-Date -Format "HH:mm:ss"), $i, $procAlive, $listening, $healthy)
        if ($healthy) { return $true }
        if ($procAlive) { $goneStreak = 0 } else { $goneStreak++ }
        if ($goneStreak -ge 5) {
            Write-Host ("[{0}] Wait-Healthy: process AIOpsServer gone for 5 probes -> fail fast" -f (Get-Date -Format "HH:mm:ss"))
            $script:WaitHealthyReason = "process-gone"
            $tail = Get-StartupLogTail -Lines 6
            if ($tail) { Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Wait-Healthy: startup.log tail: $tail" }
            return $false
        }
        if ((Get-Date) -gt $deadline) { break }
    }
    $alive = [bool](Get-Process -Name "AIOpsServer" -ErrorAction SilentlyContinue)
    $list = Test-Port8000
    Write-Host ("[{0}] Wait-Healthy: timed out (process alive={1}, port8000_listening={2})" -f (Get-Date -Format "HH:mm:ss"), $alive, $list)
    $script:WaitHealthyReason = "timeout"
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
# apply.log is opened in APPEND mode and accumulates across every upgrade ever run on
# this machine. Emit a clearly timestamped banner so runs can be told apart.
$runMode = if ($Rollback) { "rollback" } else { "upgrade" }
Write-Host ("===== AIOps {0} script START {1} | AppRoot={2} | Staging={3} =====" -f $runMode, (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $AppRoot, $Staging)

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
        Write-State -State "failed" -Progress 70 -Message "Rollback completed but health check did not pass - please verify the platform manually" -ErrorMsg "health check timeout"
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
    # Ship the on-site diagnostic helper: after an upgrade it must exist on the box even
    # if the original install never had it (it is the fast way to settle "started, then
    # vanished" without a round trip).
    if ($Staging -and (Test-Path (Join-Path $Staging "deploy\win_startup_diag.ps1"))) {
        New-Item -ItemType Directory -Force -Path (Join-Path $AppRoot "deploy") | Out-Null
        Copy-Item (Join-Path $Staging "deploy\win_startup_diag.ps1") (Join-Path $AppRoot "deploy\win_startup_diag.ps1") -Force
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
# Let the filesystem settle before the FIRST launch (2026-10-30 field report): the ~90 MB
# exe plus the whole frontend had just been copied over, and pg_dump had been hammering
# the same disk seconds earlier. An on-access AV scan of the freshly written exe is also
# still in flight. Launching at t+0 is what the failing box did; a few seconds of calm is
# far cheaper than a false "failed".
Write-Host ("[{0}] Settling 8s before first launch of the new version" -f (Get-Date -Format "HH:mm:ss"))
Start-Sleep -Seconds 8
Start-App

Write-State -State "verifying" -Progress 90 -Message "Waiting for service health"
if (Wait-Healthy) {
    Write-State -State "done" -Progress 100 -Message "Upgrade completed successfully"
    Write-Host "Upgrade done."
    exit 0
} else {
    # One retry before declaring failure (2026-10-30, field report 4.5.8 -> 4.6.1):
    # the freshly copied ~90MB exe is often the target of an on-access AV scan on its
    # FIRST execution - the process shows up, then vanishes within seconds, and a
    # second launch a moment later succeeds. A retry costs ~15s and removes that class
    # of transient false failure.
    Write-State -State "verifying" -Progress 92 -Message "First launch did not stay up - retrying once (new exe may be scanned by antivirus on first run)"
    Start-Sleep -Seconds 5
    Start-App
    if (Wait-Healthy) {
        Write-State -State "done" -Progress 100 -Message "Upgrade completed successfully (succeeded on second launch)"
        Write-Host "Upgrade done."
        exit 0
    }
    # Do NOT report progress 0 here: the files HAVE been replaced, so 0% would make the
    # page look as if nothing happened. Keep 90 and give an actionable message.
    $tail = Get-StartupLogTail -Lines 8
    $msg = "Health check did not pass after restart. The new version may still be starting - please open the platform and verify the version before rolling back."
    if ($script:WaitHealthyReason -eq "process-gone") {
        if ($tail) {
            $msg = "New version exited right after launch. Log tail (backend\logs\startup.log): $tail"
            $err = "startup crashed - see backend\logs\startup.log"
        } else {
            $msg = "New version exited right after launch and wrote no startup log - the exe may never have reached Python, or antivirus blocked the freshly copied exe. Run deploy\win_startup_diag.ps1 to capture the cause."
            $err = "process exited without log"
        }
    } elseif ($tail) {
        $msg = "New version did not answer /health. Log tail (backend\logs\startup.log): $tail"
        $err = "startup failed - see backend\logs\startup.log"
    } else {
        $err = "health check timeout"
    }
    Write-State -State "failed" -Progress 90 -Message $msg -ErrorMsg $err
    exit 1
}
