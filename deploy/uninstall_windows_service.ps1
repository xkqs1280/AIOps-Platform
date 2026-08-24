[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectDir "backend\.venv\Scripts\python.exe"
$serviceScript = Join-Path $projectDir "backend\aiops_windows_service.py"
if (-not (Get-Service -Name "AIOpsPlatform" -ErrorAction SilentlyContinue)) { Write-Host "AIOpsPlatform 服务未安装。"; exit 0 }
Stop-Service -Name "AIOpsPlatform" -Force -ErrorAction SilentlyContinue
& $python $serviceScript remove
Write-Host "AIOpsPlatform 服务已移除。"
