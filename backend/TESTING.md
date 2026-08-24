# 测试说明

在 `backend` 目录创建虚拟环境后安装测试依赖：

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

当前测试覆盖三类不依赖 PostgreSQL 或真实网络设备的逻辑：H3C 型号识别、Syslog 字段解析、服务端模板界面资源。后续 API 集成测试应使用独立的 PostgreSQL 测试库，并以环境变量覆盖 `DATABASE_URL`，不要复用生产库。
