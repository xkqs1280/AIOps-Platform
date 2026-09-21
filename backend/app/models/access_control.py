# -*- coding: utf-8 -*-
"""访问控制：平台访问 IP 白名单（总开关 + 名单条目）。

为什么做成平台内可配置：平台交付到客户现场后，「谁能打开平台」往往是硬性要求
（等保的访问控制条款、甲方只允许办公网访问）。现场不一定有能改的防火墙策略，
交付人员也不该为了加一个 IP 去改配置文件、重启服务 —— 因此做成管理员自助维护的名单。

两张新表由 ``Base.metadata.create_all`` 负责建表（无需写迁移）。
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AccessControlSetting(Base):
    """访问控制单行设置。目前只有 IP 白名单总开关。

    **默认 False = 不限制**：老部署升级上来行为完全不变，任何时候都不会因为
    「新增了这个安全功能」把现场的人挡在平台外面。
    """

    __tablename__ = "access_control_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ip_whitelist_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 最后一次修改人（审计线索；完整操作留痕另见 audit_logs）
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class IpWhitelistEntry(Base):
    """白名单条目：一行 = 单个 IP 或一个网段（CIDR）。

    ``cidr`` 统一存规范化后的文本：单 IP 存成 ``a.b.c.d/32``、网段存成 ``a.b.c.0/24``。
    这样匹配时只要 ``ip_network(cidr)`` 即可，不必再区分「用户填的是单个 IP 还是网段」。
    """

    __tablename__ = "ip_whitelist_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cidr: Mapped[str] = mapped_column(String(64), nullable=False)
    # 备注（如「办公网」「运维笔记本」）—— 现场条目一多，没备注根本认不出该删哪条
    remark: Mapped[str] = mapped_column(String(64), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
