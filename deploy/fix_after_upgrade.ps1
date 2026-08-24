# ============================================================
# AIOps - 覆盖安装后自检修复脚本（v2.6 加固版）
# 用途：覆盖安装新包后，检测/修复以下问题：
#   1) 数据库连接（.env 的 DATABASE_URL 密码与 PG 实际是否一致）
#   2) 设备凭据解密失败（CREDENTIAL_ENCRYPTION_KEY 更换导致）
#   3) 告警规则为空（init_db 未执行成功）
# 用法：双击「fix_after_upgrade.bat」→ 以管理员身份运行
# ============================================================
param([switch]$Auto)

# -Auto: 静默模式（跳过所有交互输入，便于计划任务/无人值守）
function Pause-IfInteractive { if (-not $Auto) { Read-Host "按回车继续" } }

$ErrorActionPreference = "Continue"
$Host.UI.RawUI.WindowTitle = "AIOps 覆盖安装自检修复"

# 智能定位 backend：兼容脚本在部署根目录或 deploy/ 子目录
$backend = $null
foreach ($cand in @((Join-Path $PSScriptRoot 'backend'), (Join-Path (Split-Path -Parent $PSScriptRoot) 'backend'))) {
    if (Test-Path (Join-Path $cand '.env')) { $backend = $cand; break }
}
$envFile = if ($backend) { Join-Path $backend '.env' } else { Join-Path $PSScriptRoot 'backend/.env' }

Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "   AIOps 覆盖安装后自检修复" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path $envFile)) {
    Write-Host "[X] 未找到 backend\.env" -ForegroundColor Red
    Pause-IfInteractive
    exit 1
}
$envContent = [System.IO.File]::ReadAllText($envFile)
$dbUrl = $null
foreach ($line in $envContent -split "`r?`n") {
    $line = $line.Trim()
    if ($line -match "^DATABASE_URL=(.+)$") { $dbUrl = $Matches[1].Trim() }
}
if ($dbUrl -notmatch "^postgresql\+psycopg_async://([^:]+):([^@]+)@([^:]+):(\d+)/(\w+)") {
    Write-Host "[X] DATABASE_URL 无法解析" -ForegroundColor Red
    Write-Host "    当前值: $dbUrl" -ForegroundColor Gray
    Pause-IfInteractive
    exit 1
}
$dbUser = $Matches[1]; $dbPass = $Matches[2]
$dbHost = $Matches[3]; $dbPort = $Matches[4]; $dbName = $Matches[5]

# ---------- 定位 psql ----------
function Find-Psql {
    $g = Get-Command psql -ErrorAction SilentlyContinue
    if ($g) { return $g.Source }
    # 注册表：PostgreSQL 安装记录
    foreach ($root in @("HKLM:\SOFTWARE\PostgreSQL\Installs", "HKLM:\SOFTWARE\WOW6432Node\PostgreSQL\Installs")) {
        if (Test-Path $root) {
            foreach ($k in Get-ChildItem $root) {
                try {
                    $base = (Get-ItemProperty $k.PSPath -Name "Base Directory" -ErrorAction Stop)."Base Directory"
                    $cand = Join-Path $base "bin\psql.exe"
                    if (Test-Path $cand) { return $cand }
                } catch { }
            }
        }
    }
    # 常见安装路径
    foreach ($v in @("19","18","17","16","15","14","13")) {
        foreach ($base in @("C:\Program Files\PostgreSQL", "C:\PostgreSQL", "D:\PostgreSQL", "D:\Program Files\PostgreSQL")) {
            $cand = "$base\$v\bin\psql.exe"
            if (Test-Path $cand) { return $cand }
        }
    }
    return $null
}

$psql = Find-Psql
if (-not $psql) {
    Write-Host "[X] 未找到 psql 命令。" -ForegroundColor Red
    Write-Host "    请确认 PostgreSQL 已安装，或将 psql.exe 加入 PATH 后重试。" -ForegroundColor Yellow
    Write-Host "    （本包自带 PostgreSQL 安装器，如未安装请先运行 setup/一键部署）" -ForegroundColor Yellow
    Pause-IfInteractive
    exit 1
}
Write-Host "[..] 使用 psql: $psql" -ForegroundColor Gray

# ---------- 数据库连接检测（localhost 失败自动回退 127.0.0.1） ----------
Write-Host "[1/3] 检测数据库连接（$dbUser@$dbHost`:$dbPort/$dbName）..." -ForegroundColor Cyan
$dbOk = $false
$env:PGPASSWORD = $dbPass
foreach ($tryHost in @($dbHost, "127.0.0.1")) {
    if ($dbOk) { break }
    $out = & $psql -U $dbUser -h $tryHost -p $dbPort -d $dbName -t -c "SELECT 1;" 2>&1
    if ($LASTEXITCODE -eq 0) {
        $dbOk = $true
        if ($tryHost -ne $dbHost) {
            # localhost 连不上但 127.0.0.1 可以：把 .env 的 host 改为 127.0.0.1 避免后续再踩 IPv6 坑
            $contentF = [System.IO.File]::ReadAllText($envFile)
            $contentF = [regex]::Replace($contentF, "DATABASE_URL=.*",
                "DATABASE_URL=postgresql+psycopg_async://${dbUser}:$dbPass@127.0.0.1:$dbPort/$dbName")
            [System.IO.File]::WriteAllText($envFile, $contentF, (New-Object System.Text.UTF8Encoding($false)))
            Write-Host "[..] localhost 不通，已改用 127.0.0.1 并更新 .env" -ForegroundColor Gray
        }
    }
}
Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue

if ($dbOk) {
    Write-Host "[OK] 数据库连接正常" -ForegroundColor Green
} else {
    Write-Host "[!] 数据库连接失败！" -ForegroundColor Yellow
    Write-Host "    原因：.env 中 DATABASE_URL 的密码与 PostgreSQL 实际密码不一致，或 PG 未启动。" -ForegroundColor Yellow
    Write-Host "    先确认 PostgreSQL 服务已运行（services.msc 中 postgresql-* 服务）" -ForegroundColor Yellow
    Write-Host ""
    $newPass = ""
    if (-not $Auto) { $newPass = Read-Host "    请输入你的 PostgreSQL 超级用户 ($dbUser) 实际密码（跳过直接回车则退出）" }
    if ($newPass) {
        $content2 = [System.IO.File]::ReadAllText($envFile)
        $content2 = [regex]::Replace($content2, "DATABASE_URL=.*",
            "DATABASE_URL=postgresql+psycopg_async://${dbUser}:$newPass@127.0.0.1:$dbPort/$dbName")
        [System.IO.File]::WriteAllText($envFile, $content2, (New-Object System.Text.UTF8Encoding($false)))
        Write-Host "[OK] 已更新 DATABASE_URL。请重新运行本脚本或直接启动平台。" -ForegroundColor Green
    }
    Pause-IfInteractive
    exit 0
}

# ---------- 2. 设备凭据解密检测 ----------
Write-Host "[2/3] 检测设备凭据加密状态..." -ForegroundColor Cyan
$env:PGPASSWORD = $dbPass
$encCount = & $psql -U $dbUser -h 127.0.0.1 -p $dbPort -d $dbName -t -c "SELECT count(*) FROM devices WHERE snmp_community LIKE 'enc:%' OR mgmt_password LIKE 'enc:%';" 2>$null
Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
$n = 0
$encText = (($encCount | Out-String) -replace "\D", "")
if ($encText -match "^\d+$") { $n = [int]$encText }
if ($n -gt 0) {
    Write-Host "[!] 检测到 $n 台设备的凭据为加密状态（enc:...）。" -ForegroundColor Yellow
    Write-Host "    如果备份/巡检报「设备凭据无法解密」，说明 .env 的 CREDENTIAL_ENCRYPTION_KEY" -ForegroundColor Yellow
    Write-Host "    与加密时不同（覆盖安装换了新 key）。" -ForegroundColor Yellow
    $choice = "N"
    if (-not $Auto) { $choice = Read-Host "    是否清空这些加密凭据（清空后需在设备管理重新填写）？(y/N)" }
    if ($choice -eq "y" -or $choice -eq "Y") {
        $env:PGPASSWORD = $dbPass
        & $psql -U $dbUser -h 127.0.0.1 -p $dbPort -d $dbName -c "UPDATE devices SET snmp_community = NULL, mgmt_password = NULL WHERE snmp_community LIKE 'enc:%' OR mgmt_password LIKE 'enc:%';" 2>$null | Out-Null
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
        Write-Host "[OK] 已清空 $n 台设备的加密凭据。请在设备管理中重新填写。" -ForegroundColor Green
    } else {
        Write-Host "[..] 跳过（如需保留凭据，可联系管理员找回旧 .env 的 key）。" -ForegroundColor Gray
    }
} else {
    Write-Host "[OK] 设备凭据为明文，无解密问题。" -ForegroundColor Green
}

# ---------- 3. 告警规则补种 ----------
Write-Host "[3/3] 检查告警规则..." -ForegroundColor Cyan
$env:PGPASSWORD = $dbPass
$cnt = & $psql -U $dbUser -h 127.0.0.1 -p $dbPort -d $dbName -t -c "SELECT count(*) FROM alert_rules;" 2>$null
Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
$n = 0
$cntText = (($cnt | Out-String) -replace "\D", "")
if ($cntText -match "^\d+$") { $n = [int]$cntText }
if ($n -eq 0) {
    Write-Host "[..] 告警规则为空，正在写入默认规则..." -ForegroundColor Yellow
    $env:PGPASSWORD = $dbPass
    & $psql -U $dbUser -h 127.0.0.1 -p $dbPort -d $dbName -c @"
INSERT INTO alert_rules (name, metric, condition, threshold, duration, severity, enabled, description) VALUES
('CPU 使用率过高','cpu_usage','>',90,5,'critical',true,'设备 CPU 使用率连续 5 分钟超过 90%'),
('CPU 使用率偏高','cpu_usage','>',80,10,'warning',true,'设备 CPU 使用率连续 10 分钟超过 80%'),
('内存使用率过高','memory_usage','>',90,5,'critical',true,'设备内存使用率连续 5 分钟超过 90%'),
('内存使用率偏高','memory_usage','>',80,10,'warning',true,'设备内存使用率连续 10 分钟超过 80%'),
('设备温度过高','temperature','>',70,3,'critical',true,'设备温度连续 3 分钟超过 70℃'),
('设备离线','online_status','==',0,3,'critical',true,'设备连续 3 次健康检查失败，判定离线'),
('接口带宽利用率过高','bandwidth_usage','>',85,10,'warning',true,'接口带宽利用率连续 10 分钟超过 85%'),
('接口状态异常','interface_status','==',0,1,'warning',true,'接口 down 状态'),
('配置文件变更','config_change','==',1,0,'warning',true,'检测到设备配置发生变更'),
('日志异常事件','security_event','>',0,0,'info',true,'产生安全事件日志');
"@ 2>$null | Out-Null
    Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    Write-Host "[OK] 已写入 10 条默认告警规则。" -ForegroundColor Green
} else {
    Write-Host "[OK] 告警规则已存在（$n 条），无需补种。" -ForegroundColor Green
}

Write-Host ""
Write-Host "=============================================" -ForegroundColor Green
Write-Host "   自检修复完成！" -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Green
Write-Host " 请重启平台（双击 start.bat），然后验证："
Write-Host "   - 告警规则页面应有默认规则"
Write-Host "   - 配置备份不再报「设备凭据无法解密」"
Write-Host ""
Pause-IfInteractive
