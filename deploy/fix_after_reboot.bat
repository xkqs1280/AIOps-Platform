@echo off
title AIOps 开机后恢复修复
cd /d "%~dp0.."
echo ============================================
echo   AIOps 开机后恢复修复（重启后数据库未启动时使用）
echo ============================================
echo.
echo [1/5] 检查 PostgreSQL 服务...
sc query postgresql-x64-18 >nul 2>&1
if errorlevel 1 (
    sc query postgresql-x64-17 >nul 2>&1
    if errorlevel 1 (
        sc query postgresql-x64-16 >nul 2>&1
        if errorlevel 1 (
            echo   [X] 未找到 PostgreSQL 服务，请确认已安装
            pause
            exit /b 1
        ) else ( set "PGSVC=postgresql-x64-16" )
    ) else ( set "PGSVC=postgresql-x64-17" )
) else ( set "PGSVC=postgresql-x64-18" )
echo   [OK] 服务: %PGSVC%
echo.
echo [2/5] 设置开机自启并启动...
sc config %PGSVC% start= auto >nul 2>&1
sc start %PGSVC% >nul 2>&1
echo   [OK] %PGSVC% 已设为自动并启动
echo.
echo [3/5] 等待数据库就绪（最多 60 秒）...
powershell -NoProfile -Command "$ok=$false; for($i=0;$i -lt 12;$i++){ try{$c=New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1',5432); $ok=$true; break}catch{ Start-Sleep 5} }; if(-not $ok){Write-Host 'TIMEOUT'; exit 1} else {Write-Host 'READY'}"
if errorlevel 1 (
    echo   [X] 等待超时，请手动检查 PostgreSQL 日志
    pause
    exit /b 1
)
echo   [OK] 数据库已就绪
echo.
echo.
echo [4/5] 检查设备数据（验证数据库里是否还有设备）...
set "PGBIN=%ProgramFiles%\PostgreSQL"

set "PGPASSWORD=postgres"
for /d %%v in ("%PGBIN%\*") do (
    echo   检查: %%v
    "%%v\bin\psql.exe" -w -U postgres -h localhost -d aiops -t -c "select count(*) as devices from devices;" 2>nul
)
echo.
echo [5/5] 重新启动后端...
taskkill /f /im AIOpsServer.exe >nul 2>&1
timeout /t 2 /nobreak >nul
start "" /b wscript.exe "%~dp0start_hidden.vbs"
timeout /t 8 /nobreak >nul
echo.
echo 已完成！请刷新页面查看设备数据。
pause
