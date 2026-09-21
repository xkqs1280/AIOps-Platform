from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PlatformSetting(Base):
    """平台外观设置（全局单行）：监控大屏标题等可自定义文案。

    为什么要做成可配置：平台是**交付给客户现场用的**，大屏第一眼就是那行标题，
    代理商/集成商需要把它换成「某某单位网络监控平台」。写死在代码里意味着每次
    改名都要改代码重新发版，不现实。

    ``site_name`` 为空字符串表示「未自定义」——由前端回退到内置默认标题，
    因此这里不写入任何默认值，避免把默认文案固化成历史数据（后续改默认文案时
    老部署也能跟着变）。

    新表由 ``Base.metadata.create_all`` 负责建表（无需写迁移）。
    """

    __tablename__ = "platform_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_name: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    # 最后一次修改人（审计线索；完整操作留痕另见 audit_logs）
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
