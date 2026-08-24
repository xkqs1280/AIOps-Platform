@echo off
chcp 65001 >nul
title AIOps Windows 一键打包
cd /d "%~dp0"
echo ========================================
echo   AIOps Windows 一键打包
echo   产物目录: dist\AIOps-Windows\
echo   参数: %*
echo ========================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\build_windows_exe.ps1" %*
echo.
if exist "dist\AIOps-Windows\AIOpsServer.exe" (
    echo [OK] 打包完成，部署目录：dist\AIOps-Windows\
) else (
    echo [失败] 未找到 AIOpsServer.exe，请检查上方日志。
)
pause
