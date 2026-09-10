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

[3] 离线配置合规巡检 CLI（可选，随包附带）
    tools\config_audit.py —— 纯标准库、零依赖，无需连设备也不需要授权，
    离线分析设备配置全文并按等保 2.0 规则出报告（默认打码敏感值）。
    用法：
        python tools\config_audit.py 配置.txt
        python tools\config_audit.py 配置目录\ --md 巡检报告.md
        python tools\config_audit.py --list-rules        # 查看规则清单
    规则集为同目录 config_baseline_rules.json（与平台内置规则同源）。
