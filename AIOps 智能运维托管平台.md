# AIOps 智能运维托管平台 - Windows 一键部署包

## 一、快速上手（三步）

1. **把整个文件夹复制到目标 Windows Server**（路径中不要含中文/空格，如 `D:\AIOps`）
2. 双击 **`deploy\setup.bat`** —— 自动检测/安装 Python、PostgreSQL，创建虚拟环境并安装依赖（首次约 5-10 分钟，需联网）
3. 双击 **`start.bat`** —— 启动服务，浏览器自动打开 **http://localhost:8000**

> 局域网其他电脑访问：`http://<本机IP>:8000`

## 二、目录结构

```
AIOps-部署包/
├── backend/                 # 后端源码（FastAPI）
│   └── app/                 #   应用代码（首次启动自动建表）
├── frontend/
│   └── dist/                # 前端已构建产物（免安装 Node，直接可用）
├── deploy/
│   ├── setup.bat            # ★ 一键安装（入口）
│   ├── setup.ps1            #   安装主逻辑
│   ├── start.bat            # ★ 启动服务
│   ├── stop.bat             #   停止服务
│   ├── register_autostart.bat  # 注册开机自启（系统任务计划，免装第三方工具）
│   ├── check_env.bat        #   环境自检
│   └── tools/               #   PostgreSQL 绿色版（setup 自动下载到这里）
├── requirements-win.txt     # Windows 依赖清单
└── 使用说明.md
```

## 三、脚本说明

| 脚本 | 作用 |
|------|------|
| `deploy\setup.bat` | 首次部署：装 Python(若无) → 建 venv → 装依赖 → 装 PostgreSQL(绿色版) → 初始化数据库 → 生成 .env |
| `start.bat` | 启动后端（单进程同端口托管前端，8000 端口） |
| `deploy\stop.bat` | 停止服务 |
| `deploy\register_autostart.bat` | 注册为开机自启（schtasks，Windows 自带，无需 NSSM） |
| `deploy\check_env.bat` | 环境自检（Python/PG/SNMP/venv/dist/.env） |

## 四、环境要求（能自动装的都会自动装）

| 组件 | 版本 | 说明 |
|------|------|------|
| Python | 3.10+（推荐 3.12） | 脚本自动检测；没有则自动下载安装 |
| PostgreSQL | 16+ | 脚本自动检测；没有则自动下载绿色版到 `deploy\tools\pg` |
| Net-SNMP | 不需要 | 已用内置 **pysnmp** 实现 SNMP 监控/发现，无需安装外部工具 |
| 网络 | 需能访问外网 | 安装依赖 + 外部威胁情报采集（FireHOL/ipwho.is）需要 |

## 五、注意事项

1. **路径不要含中文/空格**（venv 与依赖对路径敏感）
2. **数据库**：默认库 `aiops` / 用户 `aiops` / 密码 `aiops123`，可用 `backend\.env` 覆盖（如连已有 PG）
3. **防火墙**：放行 TCP 8000 端口（入站）；若需管理设备，出站放行设备网段的 SNMP 161 / SSH 22 / Telnet 23
4. **SNMP 工具**：不需要额外安装——平台已内置 pysnmp 实现 SNMP 采集/发现
5. **外部威胁数据**：安全监控页的实时威胁态势每 30 分钟抓取一次，需服务器能访问
   `cdn.jsdelivr.net` / `raw.githubusercontent.com` / `ipwho.is`
6. **升级**：替换 `backend/` 与 `frontend/dist/` 后重启即可；数据库表结构由 `init_db()` 自动同步
7. 生产环境建议：如需更完善的服务管理（崩溃自动拉起/多实例），可用 NSSM 替代 schtasks 注册

## 五·五、移动端 APP（Android）

平台附带原生 Android 手机 APP（APK），随时随地查看数据、接收告警通知。

- **安装**：将 `app-release.apk` 复制到手机安装（如提示未知来源，请在系统设置中允许）。
- **登录**：输入平台服务器地址与端口（默认 8000），账号密码与桌面端一致，可记住服务器地址。
- **HTTPS**：平台默认以 HTTPS（自签名证书）提供服务，APP 已内置信任证书，无需额外配置。
- **功能**：监控概览、设备、告警（中文分级）、拓扑、生命周期、等保合规、安全监控、设备巡检、账号设置。
- **告警通知**：登录后每 30 秒检查新告警；发现「严重/重要」级别新告警弹出系统通知。**首次使用请在系统设置中完全授权通知权限**（`设置 → 应用管理 → AIOps 智能运维 → 通知`，仅 APP 内弹窗授权可能不完整）。
- **升级**：覆盖安装新 APK 即可，登录状态与配置保留。

## 六、打包与发布（Windows EXE）

生成独立的 Windows 部署目录 `dist\AIOps-Windows\`：

```
AIOps-Windows/
├─ AIOpsServer.exe      # FastAPI 后端（:8000，同端口托管前端）
├─ AIOpsService.exe     # Windows 服务宿主（可选）
├─ start.bat            # 快捷启动
├─ frontend/dist/       # 已构建前端（外置，可独立升级）
├─ backend/.env         # 环境配置（自动生成随机密钥）
├─ deploy/              # 启动/停止/自启/服务注册脚本
└─ tools/               # PostgreSQL 可选依赖说明（SNMP 已内置 pysnmp）
```

| 步骤 | 命令 | 产物 |
|------|------|------|
| 一键打包 | 双击 `build.bat` | `dist\AIOps-Windows\` |
| 生成安装包 | 双击 `build_installer.bat`（需装 Inno Setup 6） | `dist\installer\AIOps-Setup.exe` |

- 构建入口：`deploy\build_windows_exe.ps1`（支持 `-Mode onefile|onedir`、`-SkipDependencyInstall`）
- PyInstaller 配置：`deploy\AIOpsServer.spec`、`deploy\AIOpsService.spec`
- 前端外置：默认不打进 exe，升级时只需替换 `frontend/dist` 与 `AIOpsServer.exe` 重启
- 首次登录：admin 初始密码随机生成并写入 `backend\.env`，登录后立即修改
- 打包目录可再分发为普通部署包（同第一~五节使用方法）
