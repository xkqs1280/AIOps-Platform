# AIOps 智能运维托管平台

面向网络与安全设备的 7×24 智能监控与运维平台。单机一体化部署，支持设备台账、实时监控、智能告警、网络拓扑、配置备份、合规巡检、安全态势、移动 APP 与一键升级。

## 功能特性

- **监控大屏**：设备在线率、告警分布、流量趋势、资源 TOP 一屏总览
- **设备管理**：台账 CRUD、批量导入导出、SNMP 自动发现、接口实时流量、序列号与硬件组件明细
- **智能告警**：CPU / 内存 / 温度 / 重启 / 接口状态 / 错包丢弃等多指标规则引擎，严重/重要/次要/警告四级中文分级，邮件通知 + 平台提示音与语音播报
- **网络拓扑**：自动连线、防自连、双击设备打开详情
- **配置备份**：设备配置定时采集、历史版本、自动清理
- **H3C 巡检**：一键巡检任务与评分
- **等保合规**：等保 2.0 三级核查项、评分与报告导出
- **安全监控**：外部威胁态势、告警联动
- **设备生命周期**：续保 / 维保提醒
- **系统升级**：平台内一键升级（上传签名升级包 → 自动备份 → 替换 → 重启 → 回滚），保留设备与数据
- **移动端 APP**：Android 原生封装，30 秒轮询 + 系统通知推送

## 技术架构

| 层 | 技术 |
|---|---|
| 后端 | Python 3.13 · FastAPI · SQLAlchemy(async) · PostgreSQL |
| 前端 | Vue 3 · Vite · TailwindCSS · ECharts |
| 移动端 | Capacitor 8 · Android（系统通知 / 本地轮询） |
| 采集 | SNMP(v2c) · SSH · Telnet · NETCONF |
| 部署 | 单机 Windows exe（PyInstaller）/ Linux uvicorn，HTTPS 自签名证书 |

## 目录结构

```
AIOps/
├─ backend/                 # FastAPI 后端
│  ├─ app/
│  │  ├─ routers/           # API 路由（auth/settings/system/devices/alerts/...）
│  │  ├─ services/          # 业务服务（采集/告警/拓扑/授权/升级...）
│  │  ├─ models/            # SQLAlchemy 模型
│  │  ├─ version.py         # 版本集中常量
│  │  └─ migrations.py      # 数据库迁移框架
│  ├─ aiops_entry.py        # 入口（自动 HTTPS / 证书检测）
│  └─ certs/                # HTTPS 证书（自行生成，不入库）
├─ frontend/
│  └─ frontend/
│     ├─ src/               # 桌面端源码（Vue3）
│     └─ mobile/            # 移动端源码（Capacitor）
├─ mobile-app/              # Android 工程（可选，由 mobile/ 构建）
├─ deploy/                  # 部署脚本（start/stop/升级/自启）
├─ tools/                   # 辅助工具（升级包制作/授权生成等）
└─ .env.example             # 环境变量模板
```

## 快速启动（开发环境）

### 前置
- Python 3.13+
- Node.js 18+
- PostgreSQL 16+（默认库 `aiops` / 用户 `aiops` / 密码 `aiops123`，可用 `.env` 覆盖）

### 1. 后端

```bash
cd backend
pip install -r ../requirements-win.txt    # 或 requirements-build.txt
cp ../.env.example .env                   # 按需修改 DATABASE_URL 等
python aiops_entry.py                     # 默认 HTTPS 8000（无证书自动回退 HTTP）
```

### 2. 前端

```bash
cd frontend/frontend
npm install
npm run dev                               # 默认 5173，已配置代理到后端 8000
```

### 3. 登录

浏览器打开 `http://localhost:5173`，默认管理员账号 `admin`，初始密码见 `.env` 的 `BOOTSTRAP_ADMIN_PASSWORD`（首次启动自动创建），登录后请立即修改。

## 环境变量（.env.example）

| 变量 | 说明 |
|---|---|
| `DATABASE_URL` | PostgreSQL 连接串 |
| `SECRET_KEY` | JWT 签名密钥 |
| `CREDENTIAL_ENCRYPTION_KEY` | 设备凭据加密密钥（FerNet），**升级/迁移时切勿更换** |
| `BOOTSTRAP_ADMIN_PASSWORD` | 首次启动创建的管理员密码 |
| `COOKIE_SECURE` | HTTPS 环境下建议 `true` |

## 生产部署

- **Windows**：使用 `deploy/build_windows_exe.ps1` 构建单文件部署包（含前端、证书、升级脚本），解压后运行 `deploy/start.bat`。
- **Linux**：`uvicorn app.main:app --host 0.0.0.0 --port 8000`（可加 `--ssl-*` 参数启用 HTTPS）。

## 升级

平台内置「系统设置 → 系统升级」：上传厂商签名升级包（`tools/build_upgrade_package.py` 制作）→ 自动备份程序/配置/数据库快照 → 替换 → 重启 → 健康自检 → 失败自动回滚。数据（PostgreSQL）与 `.env` 加密密钥全程保留。

> 升级包校验使用 RSA-SHA256 签名，签名私钥由厂商保管（不在本仓库内），可自行用 `tools/generate_license.py` 生成自己的密钥对替换。

## 许可证

[MIT](LICENSE)

## 免责声明

本项目为通用网络运维管理工具，不包含任何特定厂商的专有协议实现。请确保你有权对目标设备执行 SNMP/SSH 等采集操作，并自行评估在生产环境使用。作者不对使用本项目产生的任何后果承担责任。
