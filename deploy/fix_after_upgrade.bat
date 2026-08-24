@echo off
title AIOps 覆盖安装自检修复
echo.
echo ============================================
echo   AIOps 覆盖安装后自检修复
echo   检测数据库连接 / 设备凭据 / 告警规则
echo ============================================
echo.
echo 建议以管理员身份运行（右键 - 以管理员身份运行）。
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix_after_upgrade.ps1"
