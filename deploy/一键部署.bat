@echo off
title AIOps 平台一键部署
echo.
echo 正在启动 AIOps 平台一键部署...
echo （将自动安装 PostgreSQL，首次约 5-10 分钟）
echo.
call "%~dp0deploy\one-click-install.bat"
