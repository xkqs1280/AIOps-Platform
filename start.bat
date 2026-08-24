@echo off
chcp 65001 >nul
title AIOps 启动
cd /d "%~dp0"

REM ---- 检查是否已安装 ----
if not exist "backend\.venv\Scripts\python.exe" (
    echo [错误] 尚未安装环境，请先运行 deploy\setup.bat
    pause
    exit /b 1
)

REM ---- 启动后端（8000 端口，同端口托管前端）----
start "AIOps-Backend" /min cmd /c "cd /d %~dp0backend && .venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > ..\deploy\backend.log 2>&1"

echo 后端启动中（端口 8000）...
echo 等待 8 秒后自动打开浏览器...
timeout /t 8 /nobreak >nul
start http://localhost:8000

echo.
echo 服务已启动：
echo   - 平台地址: http://localhost:8000   （局域网访问用 http://本机IP:8000）
echo   - 后端日志: deploy\backend.log
echo   - 停止服务: 双击 deploy\stop.bat
echo.
pause
