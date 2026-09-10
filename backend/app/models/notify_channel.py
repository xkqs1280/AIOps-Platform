"""告警通知通道模型 —— 钉钉 / 企业微信 / 飞书 / 自定义 Webhook。

此前平台只有 SMTP 邮件一种告警出口，而国内客户日常依赖的是群机器人。
每个通道一行配置，全局生效（与 mail_settings 的语义一致）：
告警触发时遍历所有 enabled 通道并发推送，按 min_severity 过滤。

安全：webhook_url 中的 token/key 属于凭据，落库用 Fernet 加密；
secret（钉钉加签密钥等）同样加密，回显仅给掩码。
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# 支持的通道类型
CHANNEL_TYPES = ("dingtalk", "wecom", "feishu", "webhook")

# 严重级别排序（数值越大越严重），用于 min_severity 过滤
SEVERITY_ORDER = {"info": 0, "warning": 1, "minor": 2, "major": 3, "critical": 4}

# 各通道中文名
CHANNEL_LABELS = {
    "dingtalk": "钉钉群机器人",
    "wecom": "企业微信群机器人",
    "feishu": "飞书群机器人",
    "webhook": "自定义 Webhook",
}


class NotifyChannel(Base):
    __tablename__ = "notify_channels"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    channel_type: Mapped[str] = mapped_column(String(16), nullable=False, default="webhook")
    # 加密存储（Fernet），含 access_token 等凭据
    webhook_url: Mapped[str] = mapped_column(String(1024), default="")
    # 加密存储：钉钉加签 secret / 自定义 webhook 的签名密钥
    secret: Mapped[str] = mapped_column(String(512), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 只推送 >= 该级别的告警（warning/minor/major/critical）
    min_severity: Mapped[str] = mapped_column(String(16), default="warning", nullable=False)
    # 是否 @所有人（钉钉/企微支持）
    mention_all: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 发送失败重试次数
    retry_times: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_result: Mapped[str | None] = mapped_column(String(255))
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
