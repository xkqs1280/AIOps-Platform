[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$SkipDependencyInstall,
    [ValidateSet("onefile", "onedir")][string]$Mode = "onefile"
)

<#
.SYNOPSIS
    一键构建 AIOps Windows 部署目录（dist\AIOps-Windows\）。

.DESCRIPTION
    1) 用 PyInstaller spec 构建 AIOpsServer.exe（后端）与 AIOpsService.exe（Windows 服务宿主）
    2) 组装 AIOps-Windows/ 目录：
         AIOpsServer.exe / AIOpsService.exe（根目录）
         frontend/dist/     —— 已构建前端（外置，可独立升级）
         backend/.env       —— 自动生成随机密钥的部署配置
         deploy/            —— 启动/停止/自启/服务注册脚本
         tools/             —— PostgreSQL 可选依赖说明
    3) 复制安全与部署文档

    默认 onefile 单文件模式（AIOpsServer.exe 在根目录）；-Mode onedir 生成目录版并摊平到根。

.PARAMETER Python
    python 可执行文件（默认 "python"，可传完整路径）。

.PARAMETER SkipDependencyInstall
    跳过依赖安装（构建机已装好 requirements 时使用）。

.PARAMETER Mode
    onefile（默认，单文件 exe）或 onedir（目录版，启动更快，杀软误报更低）。
#>

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $projectDir "backend"
$frontendDist = Join-Path $projectDir "frontend\dist"
$releaseDir = Join-Path $projectDir "dist\AIOps-Windows"
$workDir = Join-Path $projectDir "build\pyinstaller"
$specServer = Join-Path $PSScriptRoot "AIOpsServer.spec"
$specService = Join-Path $PSScriptRoot "AIOpsService.spec"

if (-not (Test-Path $frontendDist)) { throw "缺少前端构建产物：$frontendDist" }
if (-not (Test-Path $specServer)) { throw "缺少 PyInstaller spec：$specServer" }
if (-not (Test-Path $specService)) { throw "缺少 PyInstaller spec：$specService" }

if (-not $SkipDependencyInstall) {
    Write-Host "[1/5] 安装构建依赖..." -ForegroundColor Cyan
    & $Python -m pip install -r (Join-Path $projectDir "requirements-win.txt") -r (Join-Path $projectDir "requirements-build.txt")
    if ($LASTEXITCODE -ne 0) { throw "构建依赖安装失败。" }
}

# 覆盖安装保护：清空前备份旧 .env 的 CREDENTIAL_ENCRYPTION_KEY，
# 构建后复用该 key，避免更换 key 导致库中已加密凭据无法解密。
$backupEnvFile = Join-Path $env:TEMP "AIOps_old_env_key.txt"
if (Test-Path (Join-Path $releaseDir "backend\.env")) {
    $oldEnv = [System.IO.File]::ReadAllText((Join-Path $releaseDir "backend\.env"))
    $m = [regex]::Match($oldEnv, "(?m)^CREDENTIAL_ENCRYPTION_KEY=(.+)$")
    if ($m.Success -and $m.Groups[1].Value.Trim()) {
        [System.IO.File]::WriteAllText($backupEnvFile, $m.Groups[1].Value.Trim(), (New-Object System.Text.UTF8Encoding($false)))
        Write-Host "[..] 已备份旧 CREDENTIAL_ENCRYPTION_KEY（覆盖安装保留凭据可解密）" -ForegroundColor Cyan
    }
} else {
    Remove-Item $backupEnvFile -ErrorAction SilentlyContinue
}

Remove-Item -Recurse -Force $releaseDir, $workDir -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $releaseDir, $workDir | Out-Null

# 环境变量驱动 spec 的 onefile/onedir 分支
$env:AIOPS_BUILD_MODE = $Mode

Write-Host "[2/5] 构建 AIOpsServer.exe（Mode=$Mode）..." -ForegroundColor Cyan
# 注：给定 .spec 文件时 PyInstaller 不允许 --specpath（那是 makespec 选项），只传 distpath/workpath
$oldEA = $ErrorActionPreference
$ErrorActionPreference = "Continue"   # PyInstaller 以管理员运行时 stderr 打 DEPRECATION，避免被 Stop 中断
& $Python -m PyInstaller --noconfirm --clean --distpath $releaseDir --workpath $workDir $specServer 2>&1 | Out-Host
$ErrorActionPreference = $oldEA
if ($LASTEXITCODE -ne 0) { throw "AIOpsServer.exe 构建失败。" }

# onedir 摊平：AIOpsServer/AIOpsServer.exe 与 AIOpsServer/_internal 移到根目录
if ($Mode -eq "onedir") {
    $serverSub = Join-Path $releaseDir "AIOpsServer"
    if (Test-Path (Join-Path $serverSub "AIOpsServer.exe")) {
        Move-Item (Join-Path $serverSub "AIOpsServer.exe") (Join-Path $releaseDir "AIOpsServer.exe") -Force
        if (Test-Path (Join-Path $serverSub "_internal")) {
            Move-Item (Join-Path $serverSub "_internal") (Join-Path $releaseDir "_internal") -Force
        }
        Remove-Item -Recurse -Force $serverSub -ErrorAction SilentlyContinue
    }
}

Write-Host "[3/5] 构建 AIOpsService.exe（Windows 服务宿主）..." -ForegroundColor Cyan
$oldEA = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $Python -m PyInstaller --noconfirm --clean --distpath $releaseDir --workpath $workDir $specService 2>&1 | Out-Host
$ErrorActionPreference = $oldEA
if ($LASTEXITCODE -ne 0) { throw "AIOpsService.exe 构建失败。" }

Write-Host "[4/5] 组装部署目录..." -ForegroundColor Cyan

# ---- frontend/dist（外置） ----
Copy-Item -Recurse -Force $frontendDist (Join-Path $releaseDir "frontend\dist")

# ---- backend/.env：从模板生成随机密钥 ----
function New-RandomString([int]$length) {
    $chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    -join (1..$length | ForEach-Object { $chars[(Get-Random -Maximum $chars.Length)] })
}
function New-FernetKey {
    # 生成 Python cryptography.Fernet 可接受的 32-byte base64url key（必须保留 padding）
    # 注意：不能去掉 "="，否则 Fernet() 抛 ValueError（43 字符无 padding 无效）
    $bytes = New-Object byte[] 32
    $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
    $rng.GetBytes($bytes)
    $rng.Dispose()
    [Convert]::ToBase64String($bytes).Replace("+", "-").Replace("/", "_")
}
function New-StrongPassword {
    # 满足 require_password_strength：>=12 位，含大小写字母和数字
    $lower = "abcdefghijklmnopqrstuvwxyz"
    $upper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    $digits = "0123456789"
    $all = $lower + $upper + $digits
    $pwd = ($lower[(Get-Random -Maximum 26)]) + ($upper[(Get-Random -Maximum 26)]) + ($digits[(Get-Random -Maximum 10)])
    $pwd += -join (1..11 | ForEach-Object { $all[(Get-Random -Maximum $all.Length)] })
    $pwd
}

# 管理员初始密码：随机强密码写入 .env（首次登录后应修改，不再使用固定默认密码）
$adminPassword = New-StrongPassword
$envTemplate = Get-Content (Join-Path $projectDir ".env.example") -Raw
# 覆盖安装保护：复用构建前备份的 CREDENTIAL_ENCRYPTION_KEY（若存在），
# 避免更换 key 导致数据库中已加密的设备凭据无法解密（InvalidToken）。
$existingKey = $null
if (Test-Path $backupEnvFile) {
    $existingKey = [System.IO.File]::ReadAllText($backupEnvFile).Trim()
}
$fernetKey = if ($existingKey) { $existingKey } else { (New-FernetKey) }

$envContent = $envTemplate `
    -replace "CHANGE_ME", "postgres" `
    -replace "postgresql\+psycopg_async://[^@]+@", "postgresql+psycopg_async://aiops:aiops123@" `
    -replace "replace-with-a-long-random-secret", (New-RandomString 48) `
    -replace "replace-with-a-fernet-key", $fernetKey `
    -replace "replace-with-a-long-random-ingest-key", (New-RandomString 32) `
    -replace "replace-with-a-strong-admin-password", $adminPassword `
    -replace "SSH_KNOWN_HOSTS=.*", "SSH_KNOWN_HOSTS=" `
    -replace "SSH_STRICT_HOST_KEY_CHECKING=.*", "SSH_STRICT_HOST_KEY_CHECKING=false" `
    -replace "COOKIE_SECURE=.*", "COOKIE_SECURE=true" `
    -replace "CORS_ORIGINS=.*", "CORS_ORIGINS=*"
if ($existingKey) {
    Write-Host "[OK] 复用已有 CREDENTIAL_ENCRYPTION_KEY（保留设备凭据可解密）" -ForegroundColor Green
} else {
    Write-Host "[OK] 生成新的 CREDENTIAL_ENCRYPTION_KEY" -ForegroundColor Green
}

$backendRelease = Join-Path $releaseDir "backend"
New-Item -ItemType Directory -Force -Path $backendRelease | Out-Null
[System.IO.File]::WriteAllText((Join-Path $backendRelease ".env"), $envContent, (New-Object System.Text.UTF8Encoding($false)))
Copy-Item (Join-Path $projectDir ".env.example") (Join-Path $backendRelease ".env.example")

# ---- HTTPS 自签名证书：backend/certs/（移动 APP 内置信任同一证书） ----
$certsRelease = Join-Path $backendRelease "certs"
New-Item -ItemType Directory -Force -Path $certsRelease | Out-Null
$srcCert = Join-Path $projectDir "backend\certs\server.crt"
$srcKey = Join-Path $projectDir "backend\certs\server.key"
if ((Test-Path $srcCert) -and (Test-Path $srcKey)) {
    Copy-Item $srcCert (Join-Path $certsRelease "server.crt") -Force
    Copy-Item $srcKey (Join-Path $certsRelease "server.key") -Force
    Write-Host "[OK] 已内置 HTTPS 自签名证书（backend/certs/，移动 APP 信任同一证书）" -ForegroundColor Green
} else {
    Write-Host "[!] 未找到 HTTPS 证书（backend/certs/server.crt），生产包将以 HTTP 运行" -ForegroundColor Yellow
}

# ---- deploy/ 运行脚本（exe 版） ----
$deployDir = Join-Path $releaseDir "deploy"
New-Item -ItemType Directory -Force -Path $deployDir | Out-Null
# bat 必须用 GBK(936) 编码写入：生产服务器 cmd 默认 GBK 代码页解析批处理，
# 若用 UTF-8 无 BOM 写含中文的 .bat，中文会被拆成乱码命令导致「不是内部或外部命令」。
# 行尾必须为 CRLF：LF 行尾会让 cmd 把含中文的行错误解析成命令。
function Write-BatFile([string]$path, [string]$content) {
    $content = $content -replace "`r?`n", "`r`n"
    [System.IO.File]::WriteAllText($path, $content, [System.Text.Encoding]::GetEncoding(936))
}

Write-BatFile (Join-Path $deployDir "start.bat") @'
@echo off
title AIOps 启动
cd /d "%~dp0.."
if not exist "AIOpsServer.exe" (
    echo [错误] 未找到 AIOpsServer.exe
    pause
    exit /b 1
)
REM 隐藏窗口启动后端（避免误关控制台窗口导致服务停止）
start "" /b wscript.exe "%~dp0start_hidden.vbs"
echo 后端启动中（端口 8000）...
timeout /t 8 /nobreak >nul
start https://localhost:8000
echo.
echo 服务已启动：
echo   - 平台地址: https://localhost:8000   （局域网访问用 https://本机IP:8000）
echo   - 首次访问提示证书不受信任，请选择「继续前往」（自签名证书）
echo   - 停止服务: 双击 deploy\stop.bat
echo.
pause
'@

Write-BatFile (Join-Path $deployDir "stop.bat") @'
@echo off
title AIOps 停止
echo 正在停止 AIOps 服务...
taskkill /f /im AIOpsServer.exe >nul 2>&1
taskkill /f /im AIOpsService.exe >nul 2>&1
echo 已停止。
pause
'@

Write-BatFile (Join-Path $deployDir "autostart.bat") @'
@echo off
rem AIOps autostart wrapper: wait for PostgreSQL, launch backend, verify port 8000 and retry.
rem Called by the AIOpsBackend scheduled task (onstart / SYSTEM).
set "DEPLOY=%~dp0"
set "BASE=%~dp0.."
set "LOG=%BASE%\autostart.log"
echo [%date% %time%] autostart begin >> "%LOG%"

rem --- Wait for PostgreSQL (max 150s, 5s interval) ---
set /a n=0
:waitpg
netstat -an | findstr ":5432 " | findstr "LISTENING" >nul
if not errorlevel 1 goto launch
set /a n+=1
if %n% geq 30 goto launch
timeout /t 5 /nobreak >nul
goto waitpg

:launch
cd /d "%BASE%"
echo [%date% %time%] launching AIOpsServer.exe >> "%LOG%"
start "" wscript.exe "%DEPLOY%start_hidden.vbs"

rem --- Verify port 8000; retry once if not up within 90s ---
set /a m=0
:check
netstat -an | findstr ":8000 " | findstr "LISTENING" >nul
if not errorlevel 1 goto ok
set /a m+=1
if %m% geq 18 goto retry
timeout /t 5 /nobreak >nul
goto check
:retry
echo [%date% %time%] port 8000 not up, retrying >> "%LOG%"
start "" wscript.exe "%DEPLOY%start_hidden.vbs"
goto check
:ok
echo [%date% %time%] platform is up (port 8000) >> "%LOG%"
exit /b 0
'@

Write-BatFile (Join-Path $deployDir "register_autostart.bat") @'
@echo off
title AIOps 开机自启
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0register_autostart.ps1"
if errorlevel 1 (
    echo.
    echo [错误] 注册失败，请查看上方错误信息。
    echo.
    pause
    exit /b 1
)
pause
'@

Write-BatFile (Join-Path $deployDir "check_env.bat") @'
@echo off
title AIOps 环境自检
echo ========================================
echo   AIOps 环境自检
echo ========================================
echo.
echo [1] 后端程序:
if exist "%~dp0..\AIOpsServer.exe" (echo    已就绪) else (echo    缺失 AIOpsServer.exe)
echo.
echo [2] 前端构建产物:
if exist "%~dp0..\frontend\dist\index.html" (echo    已就绪) else (echo    缺失 frontend\dist\index.html)
echo.
echo [3] 环境配置:
if exist "%~dp0..\backend\.env" (echo    已生成 .env) else (echo    缺失 backend\.env)
echo.
echo [4] PostgreSQL:
pg_ctl --version 2>nul || (
    if exist "%~dp0..\tools\pg\bin\pg_ctl.exe" (echo    绿色版已就绪) else (echo    未找到 PostgreSQL)
)
echo.
echo [5] SNMP 采集:
echo    基于内置 pysnmp，无需安装 Net-SNMP
echo.
echo ========================================
pause
'@

Write-BatFile (Join-Path $deployDir "install_service.bat") @'
@echo off
title AIOps 服务安装
cd /d "%~dp0.."
if not exist "AIOpsService.exe" (
    echo [错误] 未找到 AIOpsService.exe
    pause
    exit /b 1
)
echo 正在安装 Windows 服务 AIOpsPlatform...
AIOpsService.exe install
if errorlevel 1 (echo 安装失败 & pause & exit /b 1)
sc.exe config AIOpsPlatform start= auto
sc.exe start AIOpsPlatform
echo.
echo 服务已安装并启动：AIOpsPlatform
echo 卸载服务：deploy\uninstall_service.bat
pause
'@

Write-BatFile (Join-Path $deployDir "uninstall_service.bat") @'
@echo off
title AIOps 服务卸载
sc.exe stop AIOpsPlatform >nul 2>&1
AIOpsService.exe remove
echo.
echo 服务已卸载：AIOpsPlatform
pause
'@

# 根目录快捷启动
Write-BatFile (Join-Path $releaseDir "start.bat") @'
@echo off
call "%~dp0deploy\start.bat"
'@

# ---- tools/ 可选依赖说明 ----
$toolsDir = Join-Path $releaseDir "tools"
New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
[System.IO.File]::WriteAllText(
    (Join-Path $toolsDir "README.txt"),
    "AIOps 可选外部依赖`r`n==========================`r`n`r`n[1] PostgreSQL（必需，平台数据库）`r`n    方案A：本机已装 PostgreSQL 16+，无需处理。`r`n    方案B：将绿色版解压到本目录 pg\（含 bin\pg_ctl.exe），启动前先初始化并启动。`r`n    方案C：从 https://www.postgresql.org/download/windows/ 安装。`r`n    默认库 aiops / 用户 aiops / 密码 aiops123，可用 backend\.env 的 DATABASE_URL 覆盖。`r`n`r`n[2] Net-SNMP`r`n    不需要：平台 SNMP 采集/发现已内置 pysnmp 实现，无需安装任何外部 SNMP 工具。`r`n",
    (New-Object System.Text.UTF8Encoding($false))
)

# ---- 文档 ----
Copy-Item (Join-Path $PSScriptRoot "SECURITY.md") (Join-Path $releaseDir "SECURITY.md") -ErrorAction SilentlyContinue
Copy-Item (Join-Path $PSScriptRoot "WINDOWS_DEPLOYMENT.md") (Join-Path $releaseDir "WINDOWS_DEPLOYMENT.md") -ErrorAction SilentlyContinue

[System.IO.File]::WriteAllText(
    (Join-Path $releaseDir "README.md"),
    "# AIOps 智能运维托管平台（Windows 部署包）`r`n`r`n## 三步部署`r`n1. 复制整个 AIOps-Windows 文件夹到目标服务器（路径不要含中文/空格）。`r`n2. 双击 deploy\start.bat 启动，浏览器打开 https://localhost:8000（局域网用 https://本机IP:8000，首次访问提示证书不受信任选「继续前往」）。`r`n3. 如需开机自启：deploy\register_autostart.bat；如需注册为 Windows 服务：deploy\install_service.bat。`r`n`r`n## 目录结构`r`n``````text`r`nAIOps-Windows/`r`n├─ AIOpsServer.exe      # FastAPI 后端（:8000，HTTPS，同端口托管前端）`r`n├─ AIOpsService.exe     # Windows 服务宿主（可选）`r`n├─ start.bat            # 快捷启动`r`n├─ frontend/dist/       # 已构建前端`r`n├─ backend/.env         # 环境配置（首次启动前按需修改）`r`n├─ backend/certs/       # HTTPS 自签名证书（移动 APP 内置信任）`r`n├─ deploy/              # 启动/停止/自启/服务注册脚本`r`n└─ tools/               # PostgreSQL 可选依赖（SNMP 采集已内置 pysnmp）`r`n`````` `r`n`r`n## 首次登录`r`n管理员账号 admin，初始密码已随机生成并写入 backend\.env 的 BOOTSTRAP_ADMIN_PASSWORD。`r`n首次启动后请立即修改密码。`r`n`r`n## 数据库`r`n默认库 aiops / 用户 aiops / 密码 aiops123，可用 backend\.env 覆盖（如连接已有 PG）。`r`n`r`n## 防火墙`r`n放行 TCP 8000 入站；纳管设备需出站放行 SNMP 161 / SSH 22 / Telnet 23。`r`n`r`n## 升级`r`n替换 AIOpsServer.exe 与 frontend/dist 后重启即可；表结构由 init_db() 自动同步。`r`n",
    (New-Object System.Text.UTF8Encoding($false))
)

# ---- 后处理：补齐一键部署相关文件（构建每次会清空目录，需重新组装） ----
$customDeployFiles = @(
    "one-click-install.ps1", "one-click-install.bat",
    "reset_admin.ps1", "reset_admin.bat",
    "fix_after_upgrade.ps1", "fix_after_upgrade.bat",
    "start_hidden.vbs",
    "register_autostart.ps1",
    "fix_after_reboot.bat",
    "upgrade_apply.ps1",
    "upgrade_apply.sh"
)
foreach ($f in $customDeployFiles) {
    $srcF = Join-Path $PSScriptRoot $f
    if (Test-Path $srcF) {
        Copy-Item $srcF (Join-Path $deployDir $f) -Force
    }
}
# 根目录一键部署入口（GBK 编码的 bat，从源码 deploy 复制）
$oneClickBat = Join-Path $PSScriptRoot "一键部署.bat"
if (Test-Path $oneClickBat) {
    Copy-Item $oneClickBat (Join-Path $releaseDir "一键部署.bat") -Force
}
# 环境安装包：依次查找 部署脚本同级 / 项目根同级(..\..\codex) / 项目根
$installersDir = Join-Path $toolsDir "installers"
New-Item -ItemType Directory -Force -Path $installersDir | Out-Null
foreach ($inst in @("postgresql-18.4-2-windows-x64.exe")) {
    $codexRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent  # D:\WorkBuddy\codex
    $candidates = @(
        (Join-Path $PSScriptRoot $inst),                       # deploy\
        (Join-Path $codexRoot $inst),                          # D:\WorkBuddy\codex\
        (Join-Path (Join-Path $codexRoot "tools") $inst),      # D:\WorkBuddy\codex\tools\
        (Join-Path $projectDir $inst)                          # 项目根
    )
    $srcInst = $null
    foreach ($c in $candidates) {
        if (Test-Path $c) { $srcInst = $c; break }
    }
    if ($srcInst) {
        Copy-Item $srcInst (Join-Path $installersDir $inst) -Force
    } else {
        Write-Host "[!] 未找到安装包: $inst（跳过）" -ForegroundColor Yellow
    }
}
# tools/README 与根 README：使用 deploy 目录维护的版本
$toolsReadme = Join-Path $PSScriptRoot "tools_README.txt"
if (Test-Path $toolsReadme) {
    Copy-Item $toolsReadme (Join-Path $toolsDir "README.txt") -Force
}
$rootReadme = Join-Path $PSScriptRoot "README_prod.md"
if (Test-Path $rootReadme) {
    Copy-Item $rootReadme (Join-Path $releaseDir "README.md") -Force
}

# 平台 logo 图标：打包前先生成（tools/make_icon.py，Pillow 绘制六边形拓扑图形），
# 用于 exe 图标（spec）与开始菜单快捷方式图标
$logoIco = Join-Path $projectDir "build\logo.ico"
if (-not (Test-Path $logoIco)) {
    & $Python (Join-Path $projectDir "tools\make_icon.py")
}
if (Test-Path $logoIco) {
    Copy-Item $logoIco (Join-Path $releaseDir "logo.ico") -Force
    Write-Host "[OK] 已复制平台 logo 图标" -ForegroundColor Green
}

Write-Host "[5/5] 构建完成：" -ForegroundColor Green
Write-Host "  部署目录: $releaseDir"
Write-Host "  管理员初始密码: $adminPassword  （写入 backend\.env，首次登录后请修改）" -ForegroundColor Yellow
