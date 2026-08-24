# Windows 部署

## 依赖与兼容性

- 使用 **Python 3.12 x64**。不要使用 Python 3.14；项目锁定的 NumPy、Pandas、Prophet 版本未承诺提供 3.14 的 Windows wheel。
- PostgreSQL 16+，默认端口 5432。
- SNMP 采集/发现已内置 **pysnmp**（纯 Python 实现），无需安装 Net-SNMP 命令行工具。
- `prophet` 在 Windows 的安装常受 C++ 工具链影响。`requirements-win.txt` 已移除它；预测接口需要改用服务器/Linux 容器，或另行验证 Prophet 安装。
- Redis 在当前代码中没有启动时强依赖。

## 本机安装

以管理员 PowerShell 运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\deploy\setup.ps1
```

复制 `.env.example` 内容到 `backend\.env`，并将数据库密码与 `SECRET_KEY` 改为真实安全值。手工前台运行：

```powershell
.\deploy\start.ps1
```

## 注册为自动启动的 Windows 服务

以**管理员**身份运行：

```powershell
.\deploy\install_windows_service.ps1
Get-Service AIOpsPlatform
```

服务名为 `AIOpsPlatform`，启动类型为 `Automatic`，日志写入 `deploy\backend-service.log`。停止或卸载：

```powershell
Stop-Service AIOpsPlatform
.\deploy\uninstall_windows_service.ps1
```

## Docker Desktop（Windows）

在项目根目录创建 `.env`，至少包含 `POSTGRES_PASSWORD` 与 `SECRET_KEY`，然后执行：

```powershell
docker compose up -d --build
```

Docker 方式与 Windows 服务方式二选一，不能同时绑定 8000 端口。

## 路径原则

Python 使用 `pathlib.Path`，PowerShell 使用 `$PSScriptRoot`，避免硬编码 `/` 或 `\`。服务始终将工作目录切换到 `backend`，因此 `.env` 和日志路径不依赖从哪个目录启动。
