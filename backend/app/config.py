import sys
import secrets
import os
import base64
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_backend_dir() -> Path:
    """定位后端配置目录。

    PyInstaller 打包后（frozen）：
      优先 exe 同级 backend/.env（部署目录 AIOps-Windows/backend/.env），
      其次 exe 同级 .env（兼容旧 onedir 布局）。
    源码模式：config.py 位于 backend/app/ 下，上跳 1 级到 backend/。
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / "backend" / ".env").is_file():
            return exe_dir / "backend"
        return exe_dir
    return Path(__file__).resolve().parent.parent


# In a PyInstaller build, keep editable configuration beside the executable;
# in source mode, keep it beside the backend source tree.
BACKEND_DIR = _resolve_backend_dir()

# 已知的弱占位密钥：出现在 .env 时会被自动替换为随机密钥
_WEAK_SECRETS = {
    "change-me-to-a-random-secret",
    "aiops-local-dev-secret-key-change-in-production",
    "replace-with-a-long-random-secret",
}


def _ensure_secret_key() -> None:
    """将 SECRET_KEY 持久化到 .env，保证进程重启后已签发 token 仍有效。

    - .env 已有强密钥（>=32 字符且非占位符）：复用**最后一个**强密钥，
      并清理历史重复项，避免密钥轮换导致会话失效、文件膨胀；
    - 缺失或仅为弱占位符：生成随机密钥，替换掉占位符行（无占位符则追加）。
    仅在 .env 不可写时退化为进程内随机密钥（此时重启会使已登录会话失效）。
    """
    env_file = BACKEND_DIR / ".env"
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.is_file() else []
    except OSError:
        lines = []

    key_line_idxs = [i for i, l in enumerate(lines) if l.strip().startswith("SECRET_KEY=")]
    strong = []  # (行号, 密钥值)
    for i in key_line_idxs:
        v = lines[i].strip().partition("=")[2].strip().strip('"').strip("'")
        if v and v not in _WEAK_SECRETS and len(v) >= 32:
            strong.append((i, v))

    if strong:
        # 已存在强密钥：清理重复的 SECRET_KEY 行，只保留最后一个，避免累积/轮换
        keep_idx, _ = strong[-1]
        new_lines = [l for i, l in enumerate(lines) if i not in key_line_idxs or i == keep_idx]
        if len(new_lines) != len(lines):
            try:
                env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            except OSError:
                pass
        return

    # 无强密钥：生成新密钥，替换首个占位符行（无占位符则追加）
    new_key = secrets.token_hex(32)
    weak_idx = key_line_idxs[0] if key_line_idxs else None
    if weak_idx is not None:
        lines[weak_idx] = f"SECRET_KEY={new_key}"
    else:
        lines.append(f"SECRET_KEY={new_key}")
    try:
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        # .env 目录只读时无法持久化，退回随机密钥；功能不受影响，仅重启后需重新登录
        pass


_ensure_secret_key()


def _generate_fernet_key() -> str:
    """生成标准 Fernet 密钥（base64url 编码 32 字节，保留 padding）。"""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def _ensure_encryption_key() -> None:
    """将设备凭据加密密钥 CREDENTIAL_ENCRYPTION_KEY 持久化到 .env。

    无密钥或为占位符时自动生成 Fernet 密钥写回，保证设备凭据默认加密存储；
    已有有效密钥则复用（换 key 会导致已加密凭据无法解密）。
    """
    env_file = BACKEND_DIR / ".env"
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.is_file() else []
    except OSError:
        lines = []

    def _write() -> None:
        try:
            env_file.parent.mkdir(parents=True, exist_ok=True)
            env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    # 从后往前找最后一个 CREDENTIAL_ENCRYPTION_KEY
    for i in range(len(lines) - 1, -1, -1):
        s = lines[i].strip()
        if s.startswith("CREDENTIAL_ENCRYPTION_KEY="):
            v = s.partition("=")[2].strip().strip('"').strip("'")
            if v and v not in ("", "replace-with-a-fernet-key"):
                return  # 已有有效密钥
            lines[i] = f"CREDENTIAL_ENCRYPTION_KEY={_generate_fernet_key()}"
            _write()
            return
    # 无该配置：追加
    lines.append(f"CREDENTIAL_ENCRYPTION_KEY={_generate_fernet_key()}")
    _write()


_ensure_encryption_key()


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+psycopg_async://aiops:aiops123@localhost:5432/aiops"
    REDIS_URL: str = "redis://localhost:6379/0"
    # 优先读取 .env 中已持久化的密钥；缺失或为弱占位符时由 _ensure_secret_key() 生成并写回。
    # 不设硬编码默认值，避免公开密钥被伪造 JWT。
    SECRET_KEY: str = Field(default_factory=lambda: secrets.token_hex(32))
    API_PREFIX: str = "/api/v1"
    # 是否开放 API 文档（/docs、/redoc、/openapi.json）。生产环境默认关闭，
    # 避免泄露 90+ API 端点结构（含设备管理/配置备份/SNMP 凭据字段 schema）辅助攻击面分析。
    # 开发调试时可在 .env 中显式开启：API_DOCS_ENABLED=true
    API_DOCS_ENABLED: bool = False
    # Comma-separated browser origins. Mobile App / H5 通过 Bearer token 跨域访问，
    # 不携带 cookie 凭据，默认允许全部来源（allow_credentials=False 保证安全）。
    # 如需收紧可改为逗号分隔的显式来源列表。
    CORS_ORIGINS: str = "*"
    CREDENTIAL_ENCRYPTION_KEY: str = ""
    DEFAULT_DEVICE_USERNAME: str = ""
    DEFAULT_DEVICE_PASSWORD: str = ""
    SSH_KNOWN_HOSTS: str = ""
    SSH_STRICT_HOST_KEY_CHECKING: bool = True
    AUTH_ENABLED: bool = True
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    BOOTSTRAP_ADMIN_USERNAME: str = ""
    BOOTSTRAP_ADMIN_PASSWORD: str = ""
    ALLOW_INSECURE_BOOTSTRAP: bool = False
    INGEST_API_KEY: str = ""
    COOKIE_SECURE: bool = False
    # 授权模块开关：true 时未激活/测试版到期会锁定平台（仅授权页可用）
    LICENSE_ENABLED: bool = True

    @property
    def cors_origins(self) -> list[str]:
        if self.CORS_ORIGINS.strip() == "*":
            return ["*"]
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    # 平台支持纳管的最大设备数量
    MAX_DEVICES: int = 300

    # Resolve this file's directory so the service does not depend on its
    # current working directory on Windows or Linux.
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")


settings = Settings()
