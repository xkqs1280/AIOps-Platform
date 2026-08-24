@echo off
title AIOps 平台一键部署
echo.
echo ============================================
echo   AIOps 平台 Windows 一键部署
echo   将自动安装 PostgreSQL 并启动
echo ============================================
echo.
echo 建议以管理员身份运行本脚本（右键 - 以管理员身份运行）。
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0one-click-install.ps1"
if errorlevel 1 (
    echo.
    echo [错误] 部署脚本执行失败，请查看上方错误信息后重试。
    echo.
    pause
)
