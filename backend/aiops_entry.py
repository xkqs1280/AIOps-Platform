"""PyInstaller entry point for the standalone AIOps server executable.

注意：必须直接 import app.main 并传入应用对象（而非 "app.main:app" 字符串），
否则 PyInstaller 静态分析看不到该引用，打包产物会缺失 app 包。

HTTPS：
  - 部署包自带 backend/certs/server.crt + server.key（自签名，内置信任到移动 APP）；
  - 检测到证书即启用 HTTPS（同端口 8000），未检测到则回退 HTTP；
  - 可用环境变量 AIOPS_SSL_CERTFILE / AIOPS_SSL_KEYFILE 显式覆盖证书路径。
"""
import os
from pathlib import Path

import uvicorn

from app.config import BACKEND_DIR
from app.main import app


def _resolve_ssl():
    """返回 (certfile, keyfile)；无证书时返回 (None, None)。"""
    cert = os.getenv("AIOPS_SSL_CERTFILE", "").strip()
    key = os.getenv("AIOPS_SSL_KEYFILE", "").strip()
    if cert and key:
        return cert, key
    certs_dir = BACKEND_DIR / "certs"
    certfile = certs_dir / "server.crt"
    keyfile = certs_dir / "server.key"
    if certfile.is_file() and keyfile.is_file():
        return str(certfile), str(keyfile)
    return None, None


if __name__ == "__main__":
    certfile, keyfile = _resolve_ssl()
    kwargs = {}
    if certfile and keyfile:
        kwargs["ssl_certfile"] = certfile
        kwargs["ssl_keyfile"] = keyfile

    uvicorn.run(
        app,
        host=os.getenv("AIOPS_HOST", "0.0.0.0"),
        port=int(os.getenv("AIOPS_PORT", "8000")),
        log_level=os.getenv("AIOPS_LOG_LEVEL", "info"),
        **kwargs,
    )
