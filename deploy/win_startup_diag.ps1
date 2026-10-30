# AIOps Windows startup diagnostic (READ-ONLY except for one controlled start)
#
# Purpose: when an upgrade finishes but the new version does not come up, this script
# collects everything needed to name the root cause, then starts the backend in the
# foreground WITH stdout/stderr captured (a hidden launch otherwise throws the traceback
# away - that is exactly why the 2026-10-30 incident left no evidence).
#
# Usage (run as Administrator, in PowerShell 5.1):
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\AIOps-Windows\deploy\win_startup_diag.ps1
#   ... -AppRoot "D:\AIOps-Windows"          (default)
#   ... -Restart                              (kill a stale AIOpsServer.exe first)
#   ... -WaitSeconds 120                      (how long to watch the port)
#
# Output: console + <AppRoot>\deploy\startup-diag-YYYYMMDD-HHmmss.log
#         (send that one file back; it contains NO secrets)
#
# NOTE: file must stay pure ASCII - PowerShell 5.1 reads UTF-8 no-BOM as ANSI.
param(
    [string]$AppRoot = "D:\AIOps-Windows",
    [int]$WaitSeconds = 90,
    [switch]$Restart
)

$ErrorActionPreference = "Continue"
$ts = Get-Date -Format "yyyyMMdd-HHmmss"
$diagDir = Join-Path $AppRoot "deploy"
if (-not (Test-Path $diagDir)) { New-Item -ItemType Directory -Force -Path $diagDir | Out-Null }
$diagLog = Join-Path $diagDir "startup-diag-$ts.log"

function Say {
    param([string]$Text)
    $line = "$(Get-Date -Format 'HH:mm:ss')  $Text"
    Write-Host $line
    Add-Content -Path $diagLog -Value $line -Encoding UTF8
}

function Section {
    param([string]$Title)
    Say ""
    Say "===== $Title ====="
}

Say "AIOps startup diagnostic | AppRoot=$AppRoot | script=win_startup_diag.ps1"

# ---------------------------------------------------------------- 1. environment
Section "1. host"
Say ("OS          : " + (Get-CimInstance Win32_OperatingSystem | ForEach-Object { "$($_.Caption) build $($_.BuildNumber)" }))
Say ("Computer    : " + $env:COMPUTERNAME + "   User: " + $env:USERNAME)
try {
    $os = Get-CimInstance Win32_OperatingSystem
    Say ("Memory free : {0:N0} MB / {1:N0} MB" -f ($os.FreePhysicalMemory / 1KB), ($os.TotalVisibleMemorySize / 1KB))
} catch { }

$driveLetter = (Split-Path -Qualifier $AppRoot)
if ($driveLetter) {
    try {
        $d = Get-PSDrive -Name ($driveLetter.TrimEnd(':')) -ErrorAction Stop
        Say ("Disk {0} free : {1:N0} MB (need >= 2000 MB for a onefile exe to unpack)" -f $driveLetter, ($d.Free / 1MB))
    } catch { }
}
try {
    $t = Get-PSDrive -Name ($env:SystemDrive.TrimEnd(':')) -ErrorAction Stop
    Say ("Disk {0} free : {1:N0} MB (TEMP lives here; a onefile exe unpacks ~250 MB)" -f $env:SystemDrive, ($t.Free / 1MB))
} catch { }

Section "2. files"
$exe = Join-Path $AppRoot "AIOpsServer.exe"
if (Test-Path $exe) {
    $f = Get-Item $exe
    $h = (Get-FileHash $exe -Algorithm SHA256).Hash
    Say ("AIOpsServer.exe : {0:N1} MB | mtime {1} | sha256 {2}" -f ($f.Length / 1MB), $f.LastWriteTime, $h.Substring(0, 16))
} else {
    Say "AIOpsServer.exe : MISSING"
}
foreach ($rel in @("backend\.env", "backend\certs\server.crt", "backend\certs\server.key", "deploy\start_hidden.vbs", "deploy\autostart.bat", "frontend\dist\index.html")) {
    $p = Join-Path $AppRoot $rel
    if (Test-Path $p) { Say ("{0} : OK ({1:N0} bytes, {2})" -f $rel, (Get-Item $p).Length, (Get-Item $p).LastWriteTime) }
    else { Say ("{0} : MISSING" -f $rel) }
}
$verFile = Join-Path $AppRoot "backend\app\version.py"
if (Test-Path $verFile) {
    $v = Select-String -Path $verFile -Pattern 'APP_VERSION|APP_BUILD_TIME' | ForEach-Object { $_.Line.Trim() }
    Say ("version.py : " + ($v -join " ; "))
} else {
    Say "version.py : MISSING (normal for a pure exe install)"
}

Section "3. config (masked)"
$envFile = Join-Path $AppRoot "backend\.env"
if (Test-Path $envFile) {
    $lines = Get-Content $envFile -ErrorAction SilentlyContinue
    Say ("env lines : " + $lines.Count)
    foreach ($l in $lines) {
        if ($l -match '^\s*#' -or $l -match '^\s*$') { continue }
        if ($l -match '^(?<k>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?<v>.*)$') {
            $k = $Matches['k']; $v = $Matches['v']
            if ($k -match 'KEY|SECRET|PASSWORD|TOKEN') {
                Say ("  {0} = <set, len={1}>" -f $k, $v.Length)
            } elseif ($k -eq 'DATABASE_URL') {
                $m = [regex]::Match($v, '@([^/]+)/([^?]+)')
                if ($m.Success) { Say ("  DATABASE_URL = ...@{0}/{1}" -f $m.Groups[1].Value, $m.Groups[2].Value) }
                else { Say "  DATABASE_URL = <set>" }
            } else {
                Say ("  {0} = {1}" -f $k, $v)
            }
        }
    }
} else {
    Say "backend\.env : MISSING"
}

Section "4. runtime"
$pg = (netstat -ano | Select-String ":5432 " | Select-String "LISTENING")
if ($pg) { Say ("port 5432 : LISTENING" ) } else { Say "port 5432 : NOT listening  <-- backend cannot start without PostgreSQL" }
$p8 = (netstat -ano | Select-String ":8000 " | Select-String "LISTENING")
if ($p8) { Say ("port 8000 : LISTENING") } else { Say "port 8000 : not listening" }
Say ("process AIOpsServer : " + ((Get-Process -Name AIOpsServer -ErrorAction SilentlyContinue | ForEach-Object { $_.Id }) -join ","))
Say ("process AIOpsService: " + ((Get-Process -Name AIOpsService -ErrorAction SilentlyContinue | ForEach-Object { $_.Id }) -join ","))
Say ("process postgres    : " + ((Get-Process -Name postgres -ErrorAction SilentlyContinue | Measure-Object).Count) + " proc(s)")

$svc = Get-Service -Name "AIOpsPlatform" -ErrorAction SilentlyContinue
if ($svc) {
    Say ("service AIOpsPlatform : " + $svc.Status)
    Say ("  sc qc : " + ((sc.exe qc AIOpsPlatform 2>&1) -join " / "))
} else {
    Say "service AIOpsPlatform : not installed (vbs/exe launch is the normal path)"
}

Section "5. last upgrade"
$stateFile = Join-Path $AppRoot "upgrade\state.json"
if (Test-Path $stateFile) { Say ("state.json : " + ((Get-Content $stateFile -Raw) -replace '\s+', ' ')) }
$applyLog = Join-Path $AppRoot "upgrade\apply.log"
if (Test-Path $applyLog) {
    $runs = (Select-String -Path $applyLog -Pattern "upgrade/rollback script START" | Measure-Object).Count
    Say ("apply.log : {0} run(s) recorded (append-only, cross-version)" -f $runs)
    Say "tail:"
    Get-Content $applyLog -Tail 12 | ForEach-Object { Say ("  " + $_) }
}

Section "6. crash records (last 6h)"
try {
    $since = (Get-Date).AddHours(-6)
    $ev = Get-WinEvent -FilterHashtable @{ LogName = 'Application'; StartTime = $since } -ErrorAction Stop |
          Where-Object { $_.Message -match 'AIOpsServer|AIOpsService|python' } |
          Select-Object -First 8
    if ($ev) {
        foreach ($e in $ev) { Say ("  [{0}] {1} : {2}" -f $e.TimeCreated, $e.ProviderName, (($e.Message -split "`n")[0])) }
    } else {
        Say "  no application error events for AIOpsServer in the last 6h"
        Say "  (a Python-level startup exception exits quietly and leaves NO event log entry)"
    }
} catch {
    Say ("  event log query failed: " + $_.Exception.Message)
}

# ---------------------------------------------------------------- 7. controlled start
Section "7. controlled start (captured stdout/stderr)"
if ($Restart) {
    Say "killing stale AIOpsServer.exe (as requested)"
    taskkill /f /im AIOpsServer.exe 2>$null | Out-Null
    Start-Sleep -Seconds 3
}
$still = (netstat -ano | Select-String ":8000 " | Select-String "LISTENING")
if ($still) {
    Say "port 8000 is already LISTENING - backend appears to be running, skip the start test."
    Say "if the page is broken, open https://127.0.0.1:8000/health manually and report the result."
} else {
    $capture = Join-Path $diagDir "startup-capture-$ts.log"
    if (-not (Test-Path $exe)) {
        Say "cannot start: AIOpsServer.exe missing"
    } else {
        Say "starting: $exe"
        Say ("  cwd    : " + $AppRoot)
        Say ("  capture-> " + $capture)
        # NOTE: do NOT use Start-Process -RedirectStandardOutput here. PS 5.1 rebuilds a
        # case-insensitive environment dictionary and throws "duplicate key HTTPS_PROXY /
        # https_proxy" on machines that define both (very common behind a proxy).
        # cmd /c with redirection has no such problem and captures stdout+stderr together.
        $started = Get-Date
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = "$env:SystemRoot\System32\cmd.exe"
        $psi.Arguments = "/c `"`"$exe`" > `"$capture`" 2>&1`""
        $psi.WorkingDirectory = $AppRoot
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        try {
            $proc = [System.Diagnostics.Process]::Start($psi)
        } catch {
            $proc = $null
            Say ("  start failed: " + $_.Exception.Message)
        }
        if ($proc) {
            Say ("  cmd pid: " + $proc.Id)
            $deadline = (Get-Date).AddSeconds($WaitSeconds)
            $ok = $false
            $diedAfter = $null
            while ((Get-Date) -lt $deadline) {
                Start-Sleep -Seconds 3
                $alive = [bool](Get-Process -Name AIOpsServer -ErrorAction SilentlyContinue)
                $listening = [bool](netstat -ano | Select-String ":8000 " | Select-String "LISTENING")
                Say ("  probe: alive=$alive listening=$listening")
                if ($listening) { $ok = $true; break }
                if (-not $alive) { $diedAfter = ((Get-Date) - $started).TotalSeconds; break }
            }
            if ($ok) {
                Say "RESULT: backend is listening on 8000 - startup OK."
                Say "next: open https://127.0.0.1:8000/ and check the version shown on the page."
            } elseif ($null -ne $diedAfter) {
                Say ("RESULT: process DIED after ~{0:N0}s - a real startup crash, not a slow start." -f $diedAfter)
            } else {
                Say "RESULT: process still alive but port 8000 never opened in the window (slow start, or stuck before binding)."
            }
        }
        if (Test-Path $capture) {
            $content = Get-Content $capture -Raw -ErrorAction SilentlyContinue
            Say ("--- captured output ({0} chars) ---" -f $content.Length)
            if ($content) {
                ($content -split "`r?`n" | Select-Object -Last 30) | ForEach-Object { Say ("  " + $_) }
            } else {
                Say "  (empty - the exe produced no output, e.g. it never reached Python)"
            }
        }
        $bootLog = Join-Path $AppRoot "backend\logs\startup.log"
        if (Test-Path $bootLog) {
            Say "--- backend\logs\startup.log (tail 25) ---"
            Get-Content $bootLog -Tail 25 | ForEach-Object { Say ("  " + $_) }
        }
    }
}

Say ""
Say "DONE. Send this file back: $diagLog"
