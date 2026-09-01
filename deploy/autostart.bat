@echo off
rem AIOps autostart wrapper: wait for PostgreSQL, launch backend, verify port 8000 and retry ONCE.
rem Called by the AIOpsBackend scheduled task (onstart / SYSTEM).
set "DEPLOY=%~dp0"
set "BASE=%~dp0.."
set "LOG=%BASE%\autostart.log"
echo [%date% %time%] autostart begin >> "%LOG%"

rem --- Kill stale instances left by previous boot (prevent process pile-up) ---
taskkill /f /im AIOpsServer.exe >nul 2>&1

rem --- Wait for PostgreSQL (max 150s, 5s interval) ---
set /a n=0
:waitpg
netstat -an | findstr ":5432 " | findstr "LISTENING" >nul
if not errorlevel 1 goto launch
set /a n+=1
if %n% geq 30 goto launch
timeout /t 5 /nobreak >nul
goto waitpg

:launch
cd /d "%BASE%"
echo [%date% %time%] launching AIOpsServer.exe >> "%LOG%"
start "" wscript.exe "%DEPLOY%start_hidden.vbs"

rem --- Verify port 8000; retry ONCE if not up within 90s ---
set /a m=0
set "RETRIED="
:check
netstat -an | findstr ":8000 " | findstr "LISTENING" >nul
if not errorlevel 1 goto ok
set /a m+=1
if %m% geq 18 goto retry
timeout /t 5 /nobreak >nul
goto check

:retry
if defined RETRIED goto giveup
set "RETRIED=1"
echo [%date% %time%] port 8000 not up, killing stale and retrying >> "%LOG%"
taskkill /f /im AIOpsServer.exe >nul 2>&1
set /a m=0
start "" wscript.exe "%DEPLOY%start_hidden.vbs"
goto check

:giveup
echo [%date% %time%] port 8000 still not up after retry, giving up >> "%LOG%"
exit /b 1

:ok
echo [%date% %time%] platform is up (port 8000) >> "%LOG%"
exit /b 0
