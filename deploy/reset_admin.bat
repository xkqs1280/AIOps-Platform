@echo off
title AIOps 重置 admin 密码
echo.
echo ============================================
echo   AIOps 重置 admin 管理员密码
echo   将把 admin 密码重置为 backend\.env 中的值
echo ============================================
echo.
echo 建议以管理员身份运行（右键 - 以管理员身份运行）。
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0reset_admin.ps1"
