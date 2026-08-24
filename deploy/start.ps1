[CmdletBinding()]
param([int]$Port = 8000)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $projectDir "backend"
$python = Join-Path $backendDir ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "未找到虚拟环境：$python。请先运行 deploy\setup.bat。" }
Set-Location $backendDir
& $python -m uvicorn app.main:app --host 0.0.0.0 --port $Port
