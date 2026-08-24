# ============================================================
# AIOps 智能运维托管平台 - Windows Server 一键部署脚本
# 功能：
#   1) 检测 PostgreSQL 18.x；未安装则拉起安装向导由用户手动安装
#      （安装时请记住 postgres 超级用户密码，脚本会自动提示输入）
#   2) 初始化数据库（用 postgres 口令建 aiops 库 / aiops 用户）
#   3) 生成 backend\.env（含数据库连接与随机密钥）
#   4) 启动平台并打开浏览器
# 用法：双击 one-click-install.bat（管理员运行），或：
#       powershell -ExecutionPolicy Bypass -File deploy\one-click-install.ps1
# ============================================================

$ErrorActionPreference = "Continue"
$Host.UI.RawUI.WindowTitle = "AIOps 平台一键部署"

$root      = Split-Path -Parent $PSScriptRoot          # AIOps-Windows/
$deployDir = $PSScriptRoot
$backend   = Join-Path $root "backend"
$installers = Join-Path $root "tools\installers"

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host "   AIOps 智能运维托管平台  Windows 一键部署" -ForegroundColor Cyan
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host "   部署目录: $root"
Write-Host ""

# ------------------------------------------------------------
# 0. 检查是否管理员（安装软件需要）
# ------------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "[!] 当前不是管理员权限，安装 PostgreSQL 可能失败。" -ForegroundColor Yellow
    Write-Host "    建议右键「以管理员身份运行」one-click-install.bat。" -ForegroundColor Yellow
    Write-Host "    按回车继续（或 Ctrl+C 取消）..." -ForegroundColor Yellow
    Read-Host
}

# ------------------------------------------------------------
# 1. PostgreSQL：检测 / 拉起安装向导手动安装（默认密码 postgres）
# ------------------------------------------------------------
function Get-PostgresBin {
    # 常见安装路径 + PATH
    $candidates = @()
    $g = Get-Command psql -ErrorAction SilentlyContinue
    if ($g) { $candidates += Split-Path $g.Source }
    $versions = @("18", "17", "16")
    foreach ($v in $versions) {
        $candidates += "C:\Program Files\PostgreSQL\$v\bin"
    }
    foreach ($p in $candidates) {
        if (Test-Path (Join-Path $p "psql.exe")) { return $p }
    }
    return $null
}

Write-Host "[1/4] 检查 PostgreSQL..." -ForegroundColor Cyan
$pgBin = Get-PostgresBin
if ($pgBin) {
    $pgVer = & (Join-Path $pgBin "psql.exe") --version 2>$null
    Write-Host "[OK] 已检测到 PostgreSQL: $pgVer (位于 $pgBin)" -ForegroundColor Green
} else {
    $pgInstaller = Join-Path $installers "postgresql-18.4-2-windows-x64.exe"
    if (-not (Test-Path $pgInstaller)) {
        Write-Host "[X] 未找到 PostgreSQL 安装包：$pgInstaller" -ForegroundColor Red
        Write-Host "    请确认部署包完整（tools\installers\ 目录）。"
        Read-Host "按回车退出"
        exit 1
    }
    Write-Host "[..] 未检测到 PostgreSQL，将拉起安装向导（请手动完成安装）..." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "==========================================================" -ForegroundColor Yellow
    Write-Host "  PostgreSQL 18.4 安装向导即将启动，请按以下要求手动安装：" -ForegroundColor Yellow
    Write-Host "  1) 保持默认选项，一路 Next" -ForegroundColor Yellow
    Write-Host "  2) 设置超级用户 (postgres) 的密码：任意强度口令均可（稍后脚本会提示你输入）" -ForegroundColor Yellow
    Write-Host "  3) 端口保持默认 5432" -ForegroundColor Yellow
    Write-Host "  4) 安装完成后关闭向导，回到本窗口继续" -ForegroundColor Yellow
    Write-Host "==========================================================" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "按回车打开安装向导（请记住你设置的 postgres 密码）"

    # 拉起安装向导（GUI 手动引导，非静默）
    try {
        Start-Process -FilePath $pgInstaller
    } catch {
        Write-Host "[X] 无法启动 PostgreSQL 安装向导: $_" -ForegroundColor Red
        Write-Host "    请手动双击运行：$pgInstaller" -ForegroundColor Yellow
        Read-Host "安装完成后按回车继续"
    }
    # 等待用户完成安装
    Write-Host "[..] 等待安装完成..." -ForegroundColor Yellow
    $pgBin = $null
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 5
        $pgBin = Get-PostgresBin
        if ($pgBin) { break }
    }
    if (-not $pgBin) {
        Write-Host "[!] 未在标准路径检测到 PostgreSQL，请确认已安装完成。" -ForegroundColor Red
        Read-Host "按回车退出"
        exit 1
    }
    # 确保服务已启动
    $svc = Get-Service -Name "postgresql-x64-18" -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -ne "Running") {
        Start-Service "postgresql-x64-18" -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
    }
    Write-Host "[OK] PostgreSQL 18.4 已安装（端口 5432）！" -ForegroundColor Green
}

# ------------------------------------------------------------
# 2. 初始化数据库（默认连接 postgres / postgres）
# ------------------------------------------------------------
Write-Host "[2/4] 初始化数据库..." -ForegroundColor Cyan
$psql = Join-Path $pgBin "psql.exe"
$dbUser = "postgres"          # 仅用于初始化（安装时设置的超级用户）
$dbName = "aiops"
$dbPort = "5432"
# 平台专用应用用户密码：
# 优先复用现有 backend\.env 中 DATABASE_URL 的密码（升级/重装密码不漂移，否则重跑会连不上库），
# 没有 .env 时才随机生成（仅字母数字，避免破坏连接串）。
$aiopsPass = $null
$envFile0 = Join-Path $backend ".env"
if (Test-Path $envFile0) {
    try {
        $envContent0 = Get-Content $envFile0 -Raw -ErrorAction Stop
        if ($envContent0 -match "postgresql\+psycopg_async://aiops:([^@]+)@") { $aiopsPass = $matches[1] }
    } catch {}
}
if (-not $aiopsPass) {
    $aiopsPass = -join ((1..20) | ForEach-Object { "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"[(Get-Random -Maximum 60)] })
}

# 连接测试：先试 trust 本地（刚安装时 pg_hba 默认 trust for local），
# 失败则依次尝试默认密码 postgres、用户手动输入的口令。所有 psql 均加 -w（禁止交互提示，避免卡住）。
$env:PGPASSWORD = ""
Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue

function Test-PgConn {
    # 返回 $true 表示能用当前 PGPASSWORD 连接上 postgres 超级用户
    & $psql -w -U postgres -h localhost -p $dbPort -t -c "SELECT 1;" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

$pgOk = $false
# 1) 本地 trust（无密码）
if (Test-PgConn) { $pgOk = $true }
# 2) 默认密码 postgres
if (-not $pgOk) {
    $env:PGPASSWORD = "postgres"
    if (Test-PgConn) { $pgOk = $true }
}
# 3) 用户手动输入 postgres 超级用户口令（最多重试 3 次）
$attempt = 0
while (-not $pgOk -and $attempt -lt 3) {
    $attempt++
    $userPass = Read-Host -Prompt "请输入 PostgreSQL 超级用户 postgres 的密码（安装时设置的口令）"
    $env:PGPASSWORD = $userPass
    if (Test-PgConn) { $pgOk = $true }
    else { Write-Host "[!] 口令无效（第 $attempt/3 次）" -ForegroundColor Yellow }
}
if (-not $pgOk) {
    Write-Host "[X] 无法连接 PostgreSQL，请确认服务已启动且密码正确。" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}
# 注意：此处【不要】删除 PGPASSWORD——下面建库/建用户/授权仍需要 postgres 口令，
# 直到全部初始化完成后（见下方建库之后第 190 行）才清除。
Write-Host "[OK] 已连接 PostgreSQL 超级用户 postgres" -ForegroundColor Green

# 创建数据库（幂等：仅当不存在时创建）
$dbExists = (& $psql -w -U postgres -h localhost -p $dbPort -t -c "SELECT 1 FROM pg_database WHERE datname='$dbName';" 2>$null | Out-String).Trim()
if ($dbExists -ne "1") {
    & $psql -w -U postgres -h localhost -p $dbPort -c "CREATE DATABASE $dbName;" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[!] 创建数据库 $dbName 失败，请检查 postgres 权限。" -ForegroundColor Yellow
    }
}

# 创建/更新专用应用用户 aiops：
# 不存在则创建，已存在则 ALTER 同步密码（确保数据库用户密码与 .env 中连接串一致）
$sqlPass = $aiopsPass.Replace("'", "''")
& $psql -w -U postgres -h localhost -p $dbPort -c "DO `$`$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='aiops') THEN CREATE ROLE aiops LOGIN PASSWORD '$sqlPass'; ELSE ALTER ROLE aiops WITH LOGIN PASSWORD '$sqlPass'; END IF; END `$`$;" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[!] 创建/更新 aiops 用户失败，请检查 postgres 权限。" -ForegroundColor Yellow
}
# owner 转移 + public schema 授权（幂等）
& $psql -w -U postgres -h localhost -p $dbPort -d $dbName -c "ALTER DATABASE $dbName OWNER TO aiops;" 2>&1 | Out-Null
& $psql -w -U postgres -h localhost -p $dbPort -d $dbName -c "GRANT ALL ON SCHEMA public TO aiops;" 2>&1 | Out-Null
Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue

# 验证连接（用专用用户 aiops）
$env:PGPASSWORD = $aiopsPass
$test = & $psql -w -U aiops -h localhost -p $dbPort -d $dbName -t -c "SELECT 1;" 2>&1
Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
if ($LASTEXITCODE -eq 0) {
    Write-Host "[OK] 数据库就绪: $dbName (用户 aiops / 端口 $dbPort)" -ForegroundColor Green
} else {
    Write-Host "[!] 数据库连接验证未通过（$test）" -ForegroundColor Yellow
    Write-Host "    将尝试继续启动平台；若平台无法连接数据库，请重新执行本脚本。" -ForegroundColor Yellow
}

# ------------------------------------------------------------
# 3. 生成 backend\.env
# ------------------------------------------------------------
Write-Host "[3/4] 生成环境配置..." -ForegroundColor Cyan
$envFile = Join-Path $backend ".env"
if (Test-Path $envFile) {
    # 已存在：保留原配置不动（升级/重装场景，避免破坏已有数据库连接与密钥）
    # 同时从现有 .env 读取管理员初始密码，保证 [4/4] 汇总能正确显示（不会出现 admin / 空）
    try {
        $envContent0 = Get-Content $envFile -Raw -ErrorAction Stop
        if ($envContent0 -match "(?m)^BOOTSTRAP_ADMIN_PASSWORD=(.+)$") { $adminPass = $matches[1].Trim() }
    } catch {}
    Write-Host "[OK] 已检测到 backend\.env，保留现有配置。" -ForegroundColor Green
} else {
    $envTemplate = Join-Path $backend ".env.example"
    if (-not (Test-Path $envTemplate)) {
        Write-Host "[X] 缺少 backend\.env.example，部署包不完整。" -ForegroundColor Red
        Read-Host "按回车退出"
        exit 1
    }
    $content = Get-Content $envTemplate -Raw
    # 管理员初始密码：随机强密码（保证含大写/小写/数字/符号），首次登录后仍建议修改
    $adminPass = ("ABCDEFGHJKLMNPQRSTUVWXYZ"[(Get-Random -Maximum 24)] +
                  "abcdefghijkmnpqrstuvwxyz"[(Get-Random -Maximum 24)] +
                  "23456789"[(Get-Random -Maximum 8)] +
                  "!@#%^&*"[(Get-Random -Maximum 8)] +
                  (-join ((1..12) | ForEach-Object { "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#%^&*"[(Get-Random -Maximum 66)] })))
    $content = $content -replace "CHANGE_ME", "postgres"
    $content = $content -replace "postgresql\+psycopg_async://[^@]+@", "postgresql+psycopg_async://aiops:$aiopsPass@"
    $content = $content -replace "replace-with-a-long-random-secret", (-join ((1..32) | ForEach-Object { "{0:x2}" -f (Get-Random -Maximum 256) }))
    # Fernet 密钥：必须保留 base64 padding（去掉会解析失败导致凭据加密不可用）
    $content = $content -replace "replace-with-a-fernet-key", ([Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Minimum 0 -Maximum 256 })) -replace "\+", "-" -replace "/", "_")
    $content = $content -replace "replace-with-a-long-random-ingest-key", (-join ((1..32) | ForEach-Object { "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"[(Get-Random -Maximum 62)] }))
    $content = $content -replace "replace-with-a-strong-admin-password", $adminPass
    # 部署机默认无 known_hosts：关闭 SSH 主机密钥强校验，避免所有 SSH 功能失败
    $content = $content -replace "SSH_KNOWN_HOSTS=.*", "SSH_KNOWN_HOSTS="
    $content = $content -replace "SSH_STRICT_HOST_KEY_CHECKING=.*", "SSH_STRICT_HOST_KEY_CHECKING=false"
    # HTTP 部署：cookie 不能标记 secure，否则登录后无法保持会话
    $content = $content -replace "COOKIE_SECURE=.*", "COOKIE_SECURE=false"
    [System.IO.File]::WriteAllText($envFile, $content, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "[OK] 已生成 backend\.env（数据库连接: aiops / 随机密码，管理员: admin / $adminPass）" -ForegroundColor Green
}

# ------------------------------------------------------------
# 4. 启动平台
# ------------------------------------------------------------
Write-Host "[4/4] 启动平台..." -ForegroundColor Cyan
$serverExe = Join-Path $root "AIOpsServer.exe"
if (Test-Path $serverExe) {
    # 隐藏窗口启动后端（避免常驻桌面控制台窗口被误关导致服务停止）
    $hiddenVbs = Join-Path $PSScriptRoot "start_hidden.vbs"
    if (Test-Path $hiddenVbs) {
        & wscript.exe $hiddenVbs
    } else {
        Start-Process -FilePath $serverExe -WorkingDirectory $root -WindowStyle Hidden
    }
    Write-Host "[OK] 平台启动中（端口 8000）..." -ForegroundColor Green
    Start-Sleep -Seconds 8
    Write-Host ""
    Write-Host "=====================================================" -ForegroundColor Green
    Write-Host "   部署完成！" -ForegroundColor Green
    Write-Host "=====================================================" -ForegroundColor Green
    Write-Host "   平台地址: https://localhost:8000" -ForegroundColor White
    Write-Host "   局域网访问: https://本机IP:8000" -ForegroundColor White
    Write-Host "   管理员账号: admin / $adminPass （首次登录后请修改）" -ForegroundColor White
    Write-Host "   数据库: 用户 aiops（端口 5432，库 aiops）" -ForegroundColor White
    Write-Host "   提示: 平台使用 HTTPS 自签名证书，浏览器首次访问会提示不受信任，点「继续前往」即可。" -ForegroundColor Gray
    Write-Host ""
    Write-Host "   停止服务: 双击 deploy\stop.bat" -ForegroundColor Gray
    Write-Host "   开机自启: 双击 deploy\register_autostart.bat" -ForegroundColor Gray
    Write-Host ""

    # 创建开始菜单快捷方式（方便快速启动，图标为平台 logo）
    try {
        $startMenuDir = Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs"
        if (-not (Test-Path $startMenuDir)) {
            $startMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
        }
        $ws = New-Object -ComObject WScript.Shell
        $lnkPath = Join-Path $startMenuDir "AIOps 智能运维.lnk"
        $lnk = $ws.CreateShortcut($lnkPath)
        $lnk.TargetPath = Join-Path $root "start.bat"
        $lnk.WorkingDirectory = $root
        $logoIco = Join-Path $root "logo.ico"
        $lnk.IconLocation = "$logoIco,0"
        $lnk.Description = "AIOps 智能运维托管平台（端口 8000）"
        $lnk.Save()
        Write-Host "[OK] 已创建开始菜单快捷方式「AIOps 智能运维」" -ForegroundColor Green
    } catch {
        Write-Host "[!] 创建开始菜单快捷方式失败（$_），可手动创建。" -ForegroundColor Yellow
    }

    # 创建桌面快捷方式（同样指向 start.bat，方便一键启动）
    try {
        $desktopDir = [Environment]::GetFolderPath('Desktop')
        if (-not (Test-Path $desktopDir)) { $desktopDir = Join-Path $env:USERPROFILE "Desktop" }
        if (Test-Path $desktopDir) {
            $ws2 = New-Object -ComObject WScript.Shell
            $lnk2Path = Join-Path $desktopDir "AIOps 智能运维.lnk"
            $lnk2 = $ws2.CreateShortcut($lnk2Path)
            $lnk2.TargetPath = Join-Path $root "start.bat"
            $lnk2.WorkingDirectory = $root
            $logoIco2 = Join-Path $root "logo.ico"
            $lnk2.IconLocation = "$logoIco2,0"
            $lnk2.Description = "AIOps 智能运维托管平台（端口 8000）"
            $lnk2.Save()
            Write-Host "[OK] 已创建桌面快捷方式「AIOps 智能运维」" -ForegroundColor Green
        } else {
            Write-Host "[!] 未找到桌面目录，跳过桌面快捷方式。" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "[!] 创建桌面快捷方式失败（$_），可手动创建。" -ForegroundColor Yellow
    }
    Write-Host ""

    Start-Process "https://localhost:8000"
} else {
    Write-Host "[X] 未找到 AIOpsServer.exe，请确认部署包完整。" -ForegroundColor Red
}

Write-Host ""
Read-Host "部署完成，按回车关闭窗口"
