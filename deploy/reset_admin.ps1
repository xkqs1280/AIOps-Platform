# ============================================================
# AIOps - 重置 admin 密码脚本
# 用途：部署完成后若 admin 登录失败（数据库里已有旧 admin 用户，
#       bootstrap 不再执行），执行本脚本把 admin 密码重置为
#       backend\.env 中 BOOTSTRAP_ADMIN_PASSWORD 的值。
#
# 原理（两种方式，自动选择）：
#   A. 平台当前只存在 admin 一个账号时：清空 users 表 → 重启平台
#      → bootstrap 按 .env 重建 admin（最可靠，无需额外依赖）
#   B. 存在多个账号时：用 python + bcrypt 直接更新 admin 密码哈希
#
# 用法：右键「重置admin密码.bat」→ 以管理员身份运行
# ============================================================
param()

$ErrorActionPreference = "Continue"
$Host.UI.RawUI.WindowTitle = "AIOps 重置 admin 密码"

$root    = Split-Path -Parent $PSScriptRoot          # AIOps-Windows/
$backend = Join-Path $root "backend"
$envFile = Join-Path $backend ".env"

Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "   AIOps 重置 admin 管理员密码" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path $envFile)) {
    Write-Host "[X] 未找到 backend\.env，请确认部署完整。" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}
$envContent = [System.IO.File]::ReadAllText($envFile)

$adminUser = "admin"
$adminPwd = $null
$dbUrl = $null
foreach ($line in $envContent -split "`r?`n") {
    $line = $line.Trim()
    if ($line -match "^BOOTSTRAP_ADMIN_PASSWORD=(.+)$") { $adminPwd = $Matches[1].Trim() }
    if ($line -match "^BOOTSTRAP_ADMIN_USERNAME=(.+)$") { $adminUser = $Matches[1].Trim() }
    if ($line -match "^DATABASE_URL=(.+)$") { $dbUrl = $Matches[1].Trim() }
}
if (-not $adminPwd) { Write-Host "[X] .env 未找到 BOOTSTRAP_ADMIN_PASSWORD。" -ForegroundColor Red; Read-Host "按回车退出"; exit 1 }
if ($dbUrl -notmatch "postgresql\+psycopg_async://([^:]+):([^@]+)@([^:]+):(\d+)/(\w+)") {
    Write-Host "[X] DATABASE_URL 无法解析。" -ForegroundColor Red; Read-Host "按回车退出"; exit 1
}
$dbUser = $Matches[1]; $dbPass = $Matches[2]
$dbHost = $Matches[3]; $dbPort = $Matches[4]; $dbName = $Matches[5]

Write-Host "  数据库 : $dbHost`:$dbPort/$dbName (用户 $dbUser)"
Write-Host "  目标账号 : $adminUser"
Write-Host "  新密码 : $adminPwd"
Write-Host ""
$confirm = Read-Host "确认重置？(y/N)"
if ($confirm -ne "y" -and $confirm -ne "Y") { Write-Host "已取消。"; exit 0 }

# 定位 psql
$psql = $null
$g = Get-Command psql -ErrorAction SilentlyContinue
if ($g) { $psql = $g.Source }
if (-not $psql) {
    foreach ($v in @("18","17","16")) {
        $cand = "C:\Program Files\PostgreSQL\$v\bin\psql.exe"
        if (Test-Path $cand) { $psql = $cand; break }
    }
}
if (-not $psql) { Write-Host "[X] 未找到 psql，请确认 PostgreSQL 已安装。" -ForegroundColor Red; Read-Host "按回车退出"; exit 1 }

$env:PGPASSWORD = $dbPass

# 统计当前用户数
$cntLine = & $psql -U $dbUser -h $dbHost -p $dbPort -d $dbName -t -c "SELECT count(*) FROM users;" 2>$null
$userCount = 0
if ($cntLine -match "\d+") { $userCount = [int]$Matches[0] }
Write-Host "  当前 users 表账号数: $userCount"

$method = "A"
if ($userCount -gt 1) { $method = "B" }

if ($method -eq "A") {
    # ── 方式 A：清空 users 表 → 重启平台 → bootstrap 重建 ──
    Write-Host ""
    Write-Host "[A] 清空 users 表（仅 admin 账号场景，重启后自动重建）..." -ForegroundColor Yellow
    & $psql -U $dbUser -h $dbHost -p $dbPort -d $dbName -c "DELETE FROM users;" 2>$null | Out-Null
    Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    # 重启平台
    $exe = Join-Path $root "AIOpsServer.exe"
    Get-Process -Name "AIOpsServer" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    if (Test-Path $exe) {
        Start-Process -FilePath $exe -WorkingDirectory $root
        Write-Host ""
        Write-Host "[OK] 已清空账号并重启平台。" -ForegroundColor Green
        Write-Host "     平台启动后，请用以下账号登录：" -ForegroundColor Green
        Write-Host "       用户名: $adminUser" -ForegroundColor White
        Write-Host "       密码  : $adminPwd" -ForegroundColor White
        Write-Host "     （首次登录后请立即修改密码）" -ForegroundColor Yellow
    } else {
        Write-Host ""
        Write-Host "[OK] 已清空账号。请手动重启平台（双击 deploy\start.bat）。" -ForegroundColor Green
        Write-Host "     登录: $adminUser / $adminPwd" -ForegroundColor White
    }
} else {
    # ── 方式 B：python + bcrypt 直接更新 admin 密码 ──
    Write-Host ""
    Write-Host "[B] 检测到多个账号，采用直接更新 admin 密码哈希..." -ForegroundColor Yellow
    $py = $null
    foreach ($c in @("python", "py")) {
        $v = & $c --version 2>$null
        if ($LASTEXITCODE -eq 0) { $py = $c; break }
    }
    $hash = $null
    if ($py) {
        $hash = & $py -c "from passlib.hash import bcrypt; print(bcrypt.hash('$adminPwd'))" 2>$null
        if (-not $hash -or $hash -notmatch "^\$2") {
            $hash = & $py -c "import bcrypt; print(bcrypt.hashpw(b'$adminPwd', bcrypt.gensalt()).decode())" 2>$null
        }
    }
    if ($hash -match "^\$2") {
        $esc = $hash.Replace("\", "\\").Replace("'", "''")
        & $psql -U $dbUser -h $dbHost -p $dbPort -d $dbName -c "UPDATE users SET password_hash='$esc', is_active=true WHERE username='$adminUser';" 2>$null | Out-Null
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
        Write-Host ""
        Write-Host "[OK] 已重置 admin 密码为: $adminPwd" -ForegroundColor Green
        Write-Host "     请登录 https://localhost:8000 验证。（若平台在运行，可能需重启）" -ForegroundColor Green
    } else {
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
        Write-Host "[!] 无法生成密码哈希（未找到 Python 或 bcrypt 库）。" -ForegroundColor Yellow
        Write-Host "    请改用方式 A：删除 users 表中除 admin 外的其他账号后重试本脚本。" -ForegroundColor Yellow
    }
}

Write-Host ""
Read-Host "按回车关闭窗口"
