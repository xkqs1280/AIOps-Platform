@echo off
title AIOps 环境自检
echo ========================================
echo   AIOps 环境自检
echo ========================================
echo.
echo [1] Python:
python --version 2>nul || echo    未找到 Python
echo.
echo [2] PostgreSQL:
pg_ctl --version 2>nul || (
    if exist "%~dp0tools\pg\bin\pg_ctl.exe" (echo    绿色版已就绪) else (echo    未找到 PostgreSQL)
)
echo.
echo [3] SNMP 采集:
echo    基于内置 pysnmp，无需安装 Net-SNMP
echo.
echo [4] 后端虚拟环境:
if exist "%~dp0..\backend\.venv\Scripts\python.exe" (echo    已创建) else (echo    未创建，请运行 setup.bat)
echo.
echo [5] 前端构建产物:
if exist "%~dp0..\frontend\dist\index.html" (echo    已就绪) else (echo    缺失 frontend\dist\index.html)
echo.
echo [6] 数据库配置:
if exist "%~dp0..\backend\.env" (echo    已生成 .env) else (echo    未生成 .env，请运行 setup.bat)
echo.
echo ========================================
pause
