@echo off
title AIOps 停止
echo 正在停止 AIOps 服务...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do (
    taskkill /f /pid %%p >nul 2>&1
)
echo 已停止（端口 8000 已释放）。
pause
