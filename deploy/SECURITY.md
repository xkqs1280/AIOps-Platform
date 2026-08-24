# 安全配置

部署前必须在 `backend/.env` 设置强随机 `SECRET_KEY` 和
`CREDENTIAL_ENCRYPTION_KEY`。首次运行 `setup.ps1` 会自动生成两者。

`CREDENTIAL_ENCRYPTION_KEY` 是 Fernet 密钥。配置后，新建或更新的设备
SNMP Community 与管理密码会以加密形式写入数据库；不要丢失该密钥，否则
已加密的凭据无法恢复。既有明文记录会在下一次编辑设备时自动迁移。

将 `CORS_ORIGINS` 设为实际访问地址的逗号分隔列表，例如：

```text
CORS_ORIGINS=https://aiops.example.com
```

Trap 和 Syslog 摄入端点按来源 IP 限制为每分钟 120 次。应用本身尚未提供
多用户登录；生产环境应在反向代理或 VPN 后部署，并仅向受信网络开放 8000。

SSH 主机密钥校验默认启用。将受管设备的公钥写入 `SSH_KNOWN_HOSTS` 指向的
known_hosts 文件；不要通过关闭 `SSH_STRICT_HOST_KEY_CHECKING` 绕过校验。

## 登录与角色

首次启动前设置 `BOOTSTRAP_ADMIN_USERNAME` 与强密码
`BOOTSTRAP_ADMIN_PASSWORD`；系统只会在用户表为空时创建该管理员。登录地址
为 `/login`。角色为 `admin`（用户管理和删除）、`operator`（读取与常规写入）
和 `viewer`（只读）。管理员可通过 `/api/v1/auth/users` 管理用户。

生产 HTTPS 环境必须设置 `COOKIE_SECURE=true`。对于设备上报的 Trap/Syslog，
设置 `INGEST_API_KEY` 并在请求中提供 `X-Ingest-Key`。
