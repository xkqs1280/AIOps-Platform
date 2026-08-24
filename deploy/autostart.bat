@echo off
rem AIOps autostart wrapper: wait for PostgreSQL, launch backend, verify port 8000 and retry.
rem Called by the AIOpsBackend scheduled task (onstart / SYSTEM).
set "DEPLOY=%~dp0"
set "BASE=%~dp0.."
set "LOG=%BASE%\autostart.log"
echo [%date% %time%] autostart begin >> "%LOG%"

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

rem --- Verify port 8000; retry once if not up within 90s ---
set /a m=0
:check
netstat -an | findstr ":8000 " | findstr "LISTENING" >nul
if not errorlevel 1 goto ok
set /a m+=1
if %m% geq 18 goto retry
timeout /t 5 /nobreak >nul
goto check
:retry
echo [%date% %time%] port 8000 not up, retrying >> "%LOG%"
start "" wscript.exe "%DEPLOY%start_hidden.vbs"
goto check
:ok
echo [%date% %time%] platform is up (port 8000) >> "%LOG%"
exit /b 0
