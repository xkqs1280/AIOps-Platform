@echo off
chcp 65001 >nul
title AIOps 安装包生成
cd /d "%~dp0"
echo ========================================
echo   AIOps 安装包生成（Inno Setup 6）
echo ========================================
echo.

REM ---- 检查部署目录 ----
if not exist "dist\AIOps-Windows\AIOpsServer.exe" (
    echo [提示] 未找到 dist\AIOps-Windows\AIOpsServer.exe
    echo        请先运行 build.bat 完成打包。
    pause
    exit /b 1
)

REM ---- 定位 ISCC.exe ----
set "ISCC="
for %%p in (
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
    "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
) do (
    if exist "%%~p" set "ISCC=%%~p"
)

if not defined ISCC (
    echo [错误] 未找到 Inno Setup 6 (ISCC.exe)。
    echo        请到 https://jrsoftware.org/isdl.php 下载安装。
    pause
    exit /b 1
)

echo 使用 ISCC: %ISCC%
echo.
"%ISCC%" "installer\AIOps.iss"
if errorlevel 1 (
    echo [失败] 安装包生成失败。
    pause
    exit /b 1
)

echo.
echo [OK] 安装包已生成：dist\installer\AIOps-Setup.exe
pause
