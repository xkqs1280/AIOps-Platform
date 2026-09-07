@echo off
rem ============================================================
rem  AIOps Android APK build (v4.4.0+)
rem  Workspace: C:\c\aiops-build  (JDK21 + Gradle 9.5 + gradle-home9 cache)
rem  Signing:   mobile-app\aiops-signing.properties (gitignored)
rem ============================================================
setlocal enabledelayedexpansion

set BASE=C:\c\aiops-build
set JAVA_HOME=%BASE%\jdk-21.0.2
set GRADLE_USER_HOME=%BASE%\gradle-home9
set ANDROID_USER_HOME=%BASE%\android-user-home
set TMP=%BASE%\tmp
set TEMP=%BASE%\tmp
set JAVA_TOOL_OPTIONS=-Djava.io.tmpdir=%BASE%\tmp

set ANDROID_DIR=%BASE%\aiops-mobile\android
set LOCKS=%GRADLE_USER_HOME%\caches\9.5.0\transforms\.internal\locks
set LOG=%BASE%\build_apk_log.txt

echo [1/3] Cleaning stale gradle locks ...
del /q /f "%LOCKS%\*.lock" >nul 2>&1

echo [2/3] Building assembleRelease (offline, up to 5 attempts) ...
cd /d "%ANDROID_DIR%"
set /a N=1
:retry
echo --- attempt %N% ---
call "%BASE%\gradle-9.5\bin\gradle.bat" assembleRelease --no-daemon --offline --console=plain -x lint -x lintVitalRelease -x lintVitalReportRelease >> "%LOG%" 2>&1
if not errorlevel 1 goto built
if %N% geq 5 goto fail
set /a N+=1
del /q /f "%LOCKS%\*.lock" >nul 2>&1
goto retry

:built
echo.
echo [3/3] BUILD OK.
echo APK: %ANDROID_DIR%\app\build\outputs\apk\release\app-release.apk
exit /b 0

:fail
echo.
echo [ERROR] Build failed after 5 attempts. See %LOG%
exit /b 1
