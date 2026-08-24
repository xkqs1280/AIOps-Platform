# ============================================================
# AIOps 智能运维托管平台 - Windows 一键部署脚本
# 作用：检测/安装 Python → 创建虚拟环境 → 安装依赖 →
#      检测/自动安装 PostgreSQL → 初始化数据库 → 生成 .env
# 用法：双击 deploy\setup.bat（或在 PowerShell 中执行本脚本）
# ============================================================
param([switch]$SkipPG)

$ErrorActionPreference = "Continue"
$deployDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$base      = Split-Path -Parent $deployDir
$backend   = Join-Path $base "backend"
$dist      = Join-Path $base "frontend\dist"
$tools     = Join-Path $deployDir "tools"
$pgDir     = Join-Path $tools "pg"

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "   AIOps 平台 Windows 一键部署" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# ---------- 1. Python ----------
$py = $null
foreach ($c in @("python", "py")) {
    $v = & $c --version 2>$null
    if ($LASTEXITCODE -eq 0 -and $v -match "3\.(1[0-9]|[2-9][0-9])") { $py = $c; break }
}
if (-not $py) {
    Write-Host "[!] 未检测到 Python 3.10+，尝试自动下载安装 Python 3.12..." -ForegroundColor Yellow
    $pyurl = "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
    $installer = Join-Path $env:TEMP "python-3.12.7-amd64.exe"
    try {
        Invoke-WebRequest -Uri $pyurl -OutFile $installer -UseBasicParsing -TimeoutSec 180
        Write-Host "[..] 静默安装中（约 1-2 分钟）..."
        Start-Process -Wait -FilePath $installer -ArgumentList "/quiet","InstallAllUsers=1","PrependPath=1","Include_pip=1"
        # 刷新 PATH
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
        $py = "python"
    } catch {
        Write-Host "[X] Python 自动安装失败。请手动安装 Python 3.12+（勾选 Add to PATH）后重试。" -ForegroundColor Red
        Write-Host "    下载: https://www.python.org/downloads/"
        exit 1
    }
}
$pyVer = & $py --version 2>$null
Write-Host "[OK] Python: $pyVer"

# ---------- 2. 虚拟环境 + 依赖 ----------
$venvPy = Join-Path $backend ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Write-Host "[..] 创建虚拟环境 backend\.venv ..."
    & $py -m venv (Join-Path $backend ".venv")
    if ($LASTEXITCODE -ne 0) { Write-Host "[X] 创建虚拟环境失败" -ForegroundColor Red; exit 1 }
}
Write-Host "[..] 安装 Python 依赖（约 2-5 分钟，首次较慢）..."
& $venvPy -m pip install --upgrade pip -q 2>$null
& $venvPy -m pip install -r (Join-Path $base "requirements-win.txt") -q --disable-pip-version-check
if ($LASTEXITCODE -ne 0) {
    Write-Host "[X] 依赖安装失败。如网络受限，可配置国内镜像后重试：" -ForegroundColor Red
    Write-Host "    $venvPy -m pip install -r requirements-win.txt -i https://pypi.tuna.tsinghua.edu.cn/simple"
    exit 1
}
Write-Host "[OK] 依赖安装完成"

# ---------- 3. PostgreSQL ----------
$pgCtl = $null
$g = Get-Command pg_ctl -ErrorAction SilentlyContinue
if ($g) { $pgCtl = $g.Source }
if (-not $pgCtl -and (Test-Path "$tools\pg\bin\pg_ctl.exe")) { $pgCtl = "$tools\pg\bin\pg_ctl.exe" }
$pgPort = 5432
$pgData = Join-Path $tools "pgdata"

if (-not $SkipPG -and -not $pgCtl) {
    Write-Host "[!] 未检测到 PostgreSQL，尝试下载绿色版（约 300MB，请耐心等待）..." -ForegroundColor Yellow
    $pgUrl = "https://get.enterprisedb.com/postgresql/postgresql-16.4-1-windows-x64-binaries.zip"
    $pgZip = Join-Path $env:TEMP "postgresql-16.4-windows-x64.zip"
    $pgUnzip = Join-Path $env:TEMP "pg_unzip"
    try {
        Invoke-WebRequest -Uri $pgUrl -OutFile $pgZip -UseBasicParsing -TimeoutSec 900
        if (Test-Path $pgUnzip) { Remove-Item -Recurse -Force $pgUnzip }
        Expand-Archive -Path $pgZip -DestinationPath $pgUnzip -Force
        New-Item -ItemType Directory -Force -Path $pgDir | Out-Null
        $srcPg = Get-ChildItem $pgUnzip -Directory | Select-Object -First 1
        Copy-Item -Recurse -Force (Join-Path $srcPg.FullName "*") "$tools\pg"
        $pgCtl = "$tools\pg\bin\pg_ctl.exe"
    } catch {
        Write-Host "[X] PostgreSQL 自动下载失败（可能网络受限）。" -ForegroundColor Red
        Write-Host "    二选一继续：" -ForegroundColor Yellow
        Write-Host "    1) 手动安装 PostgreSQL 16+ 后重新运行本脚本；"
        Write-Host "    2) 手动下载并解压到 deploy\tools\pg\ 目录：$pgUrl"
        exit 1
    }
}
if ($pgCtl) {
    Write-Host "[OK] PostgreSQL: $pgCtl"
    # 启动（未运行则启动）
    $pgRunning = $false
    try { $conn = Test-NetConnection -ComputerName localhost -Port $pgPort -WarningAction SilentlyContinue; $pgRunning = $conn.TcpTestSucceeded } catch {}
    if (-not $pgRunning) {
        if (Test-Path "$pgData\PG_VERSION") {
            Write-Host "[..] 启动已有 PG 数据目录..."
            & $pgCtl -D $pgData -l "$deployDir\pg.log" start | Out-Null
        } else {
            Write-Host "[..] 初始化 PG 数据目录（首次）..."
            $pgInit = Join-Path (Split-Path $pgCtl) "initdb.exe"
            New-Item -ItemType Directory -Force -Path $pgData | Out-Null
            & $pgInit -D $pgData -U postgres -E UTF8 -A trust | Out-Null
            & $pgCtl -D $pgData -l "$deployDir\pg.log" start | Out-Null
        }
        Start-Sleep 3
    }
    # 建库建用户
    $psql = Join-Path (Split-Path $pgCtl) "psql.exe"
    $dbUser = "aiops"; $dbPass = "aiops123"; $dbName = "aiops"
    & $psql -U postgres -h localhost -p $pgPort -c "CREATE USER $dbUser WITH PASSWORD '$dbPass';" 2>$null | Out-Null
    & $psql -U postgres -h localhost -p $pgPort -c "CREATE DATABASE $dbName OWNER $dbUser;" 2>$null | Out-Null
    & $psql -U postgres -h localhost -p $pgPort -c "GRANT ALL PRIVILEGES ON DATABASE $dbName TO $dbUser;" 2>$null | Out-Null
    Write-Host "[OK] 数据库就绪: $dbName (用户 $dbUser)"
} else {
    Write-Host "[!] 跳过 PostgreSQL 安装（使用 -SkipPG 或已跳过）。若已有 PG 请手工配置 backend\.env 的 DATABASE_URL" -ForegroundColor Yellow
}

# ---------- 4. 生成 .env ----------
$envFile = Join-Path $backend ".env"
if (-not (Test-Path $envFile)) {
    $appSecret = & $venvPy -c "import secrets; print(secrets.token_urlsafe(48))"
    $credentialKey = & $venvPy -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # 管理员初始随机密码（>=12 位，含大小写与数字），保证首次启动自动建号
    $adminPass = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ"[(Get-Random -Maximum 26)] +
                  "abcdefghijklmnopqrstuvwxyz"[(Get-Random -Maximum 26)] +
                  "0123456789"[(Get-Random -Maximum 10)] +
                  (-join ((1..9) | ForEach-Object { "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"[(Get-Random -Maximum 62)] })))
    @(
        "DATABASE_URL=postgresql+psycopg_async://$dbUser`:$dbPass@localhost:$pgPort/$dbName",
        "SECRET_KEY=$appSecret",
        "CREDENTIAL_ENCRYPTION_KEY=$credentialKey",
        "BOOTSTRAP_ADMIN_USERNAME=admin",
        "BOOTSTRAP_ADMIN_PASSWORD=$adminPass",
        "CORS_ORIGINS=http://localhost:8000,http://127.0.0.1:8000"
    ) | Set-Content -Path $envFile -Encoding UTF8
    Write-Host "[OK] 已生成 backend\.env（管理员 admin / $adminPass，请妥善保存）" -ForegroundColor Green
}

# ---------- 5. SNMP 说明 ----------
Write-Host "[OK] SNMP 监控/发现基于内置 pysnmp 实现，无需安装 Net-SNMP。" -ForegroundColor Green

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "   部署完成！" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host " 下一步："
Write-Host "   1) 双击 start.bat 启动服务"
Write-Host "   2) 浏览器访问 http://本机IP:8000"
Write-Host "   3) 如需开机自启，运行 deploy\register_autostart.bat"
Write-Host ""
