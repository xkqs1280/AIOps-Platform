[CmdletBinding()]
param([switch]$Restart)

$ErrorActionPreference = "Stop"
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "请以管理员身份运行此脚本。"
}
$projectDir = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $projectDir "backend"
$python = Join-Path $backendDir ".venv\Scripts\python.exe"
$serviceScript = Join-Path $backendDir "aiops_windows_service.py"
if (-not (Test-Path $python)) { throw "未找到 $python；请先运行 deploy\setup.bat。" }
& $python -c "import servicemanager, win32serviceutil" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $python -m pip install "pywin32>=306"
    if ($LASTEXITCODE -ne 0) { throw "pywin32 安装失败。" }
}
$existing = Get-Service -Name "AIOpsPlatform" -ErrorAction SilentlyContinue
if (-not $existing) {
    Push-Location $backendDir
    try { & $python $serviceScript install } finally { Pop-Location }
} elseif ($Restart -and $existing.Status -eq "Running") {
    Stop-Service -Name "AIOpsPlatform" -Force
}
Set-Service -Name "AIOpsPlatform" -StartupType Automatic
Start-Service -Name "AIOpsPlatform"
Write-Host "AIOpsPlatform 已安装并设置为自动启动。"
