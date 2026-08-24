@echo off
title AIOps 一键部署
echo.
echo 正在初始化 AIOps 平台环境，请耐心等待（首次约 5-10 分钟）...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
echo.
pause
