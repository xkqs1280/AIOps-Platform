@echo off
title AIOps ¿ª»ú×ÔÆô
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0register_autostart.ps1"
pause
