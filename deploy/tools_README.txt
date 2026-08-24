AIOps 可选外部依赖
==========================

[1] PostgreSQL（必需，平台数据库）
    方案A：本机已装 PostgreSQL 16+，无需处理。
    方案B：将绿色版解压到本目录 pg\（含 bin\pg_ctl.exe），启动前先初始化并启动。
    方案C：一键部署（一键部署.bat / one-click-install.ps1）会自动检测并安装
           tools\installers\postgresql-18.4-2-windows-x64.exe（已内置在部署包内）；
           也可手动运行该安装包安装。
    默认库 aiops / 用户 aiops / 密码 aiops123，可用 backend\.env 的 DATABASE_URL 覆盖。

[2] Net-SNMP
    不需要：平台 SNMP 采集/发现已内置 pysnmp 实现，无需安装任何外部 SNMP 工具。
