"""设备日志中心模型（P0）。

两张表：
  - ``device_logs``：设备运行日志（syslog）留存表。等保要求网络日志留存 ≥ 6 个月，
    设备本地缓冲区只有 512 条且重启即丢，本表是唯一的技术留存手段。
  - ``device_loghost_configs``：设备侧"远程日志主机"配置的下发记录，用于审计与回滚。

设计要点（与 ``security_events`` 的分工）：
  - ``security_events`` 面向攻防语义（源目 IP / 威胁类型 / CVE），供安全面板使用；
  - ``device_logs`` 面向设备运行语义（模块 / 助记符 / 接口 / 设备本地时间），供运维与审计。
  两者字段、保留期、增速都不同，故分开建表；同一个 UDP 入口解析后按内容分流。

时间字段有两个，用途不同，不要混用：
  - ``device_time``   设备侧时间（按 ``SYSLOG_DEVICE_TZ_OFFSET_HOURS`` 换算为 UTC 存储）——
    设备时钟可能不准/未配时区（实测 H3C 出厂 `display clock` 返回 UTC），仅供排查参考；
  - ``received_at``   平台接收时间，恒定可靠，列表默认按它排序与过滤。
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class DeviceLog(Base):
    """设备运行日志（syslog 留存）。"""

    __tablename__ = "device_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 来源设备；未纳管设备发来的日志仍保留（device_id 为空），便于发现"漏管设备"
    device_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("devices.id"), nullable=True
    )
    # 设备自报主机名（syslog 报文里的 hostname，未必等于平台里的设备名）
    hostname: Mapped[str | None] = mapped_column(String(64))
    # 日志模块 / 助记符（H3C Comware：%%10IFNET/3/PHY_UPDOWN → module=IFNET, mnemonic=PHY_UPDOWN）
    module: Mapped[str | None] = mapped_column(String(32))
    mnemonic: Mapped[str | None] = mapped_column(String(64))
    # 日志级别：0-7，取自消息体内的 ``/N/``，**不用 PRI**（H3C PRI 由 info-center 全局配置决定，
    # 与单条消息的实际级别不一致——实测 PRI 解析出 6，消息内实际是 3）
    severity: Mapped[int | None] = mapped_column(SmallInteger)
    severity_name: Mapped[str | None] = mapped_column(String(16))
    # 运维分类：link/auth/config/protocol/system/security/other
    category: Mapped[str | None] = mapped_column(String(16))
    interface: Mapped[str | None] = mapped_column(String(128))
    username: Mapped[str | None] = mapped_column(String(64))
    src_ip: Mapped[str | None] = mapped_column(String(45))
    # 去掉前缀后的消息正文
    content: Mapped[str | None] = mapped_column(Text)
    # 设备侧时间（换算为 UTC 存储）与原始时间串（保留证据，便于核对时区）
    device_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    device_time_raw: Mapped[str | None] = mapped_column(String(48))
    # 平台接收时间（可靠，默认排序/过滤字段）
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    raw_log: Mapped[str | None] = mapped_column(Text)
    # 入口来源：udp（内置接收器）/ http（外部转发）
    log_source: Mapped[str | None] = mapped_column(String(8), default="udp")

    device = relationship("Device", lazy="selectin")

    __table_args__ = (
        Index("ix_device_logs_received_at", "received_at", postgresql_ops={"received_at": "DESC"}),
        Index(
            "ix_device_logs_device_received",
            "device_id",
            "received_at",
            postgresql_ops={"received_at": "DESC"},
        ),
        Index(
            "ix_device_logs_category_received",
            "category",
            "received_at",
            postgresql_ops={"received_at": "DESC"},
        ),
        Index(
            "ix_device_logs_severity_received",
            "severity",
            "received_at",
            postgresql_ops={"received_at": "DESC"},
        ),
        Index("ix_device_logs_module", "module"),
        # 时序追加写表的 BRIN 索引：体积约为 btree 的 1/1000，按时间范围扫描效率高。
        # 仅 PostgreSQL 支持，SQLite（单元测试）会自动忽略该方言参数。
        Index("ix_device_logs_device_time_brin", "device_time", postgresql_using="brin"),
    )


class DeviceLogHostConfig(Base):
    """设备侧「远程日志主机（loghost）」下发记录。

    每台设备可有多行历史记录（按 applied_at 倒序取最新为当前生效配置），
    用于：① 审计"谁在什么时候改了什么"；② 回滚时还原下发前的 info-center 配置快照。

    ``device_id`` **可空**：设备从平台删除时只置 NULL、不删记录（变更留痕同样是审计
    证据）。因此 ``device_name`` / ``device_ip`` 在写入时冗余保存一份快照——否则设备删掉
    之后就再也说不清"这条记录到底改的是哪台设备"。
    """

    __tablename__ = "device_loghost_configs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("devices.id"), nullable=True
    )
    # 写入时的设备名/IP 快照（设备被删除后仍可辨认）
    device_name: Mapped[str | None] = mapped_column(String(128))
    device_ip: Mapped[str | None] = mapped_column(String(45))
    loghost_address: Mapped[str | None] = mapped_column(String(64))
    loghost_port: Mapped[int | None] = mapped_column(Integer)
    # applied / rolled_back / unverified / failed
    #   unverified = 命令已下发且无报错，但配置回读不可信，无法确认是否生效。
    #   单独成一档而不是并进 failed：读不到 ≠ 没配上，混为一谈会让操作人重复下发。
    status: Mapped[str | None] = mapped_column(String(16), default="applied")
    # 下发前后的 info-center 相关配置快照（用于回滚与差异比对）
    before_config: Mapped[str | None] = mapped_column(Text)
    after_config: Mapped[str | None] = mapped_column(Text)
    # 实际下发的命令（逐行，便于审计）
    commands: Mapped[str | None] = mapped_column(Text)
    tz_offset_hours: Mapped[float | None] = mapped_column(Float)
    message: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operator: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    device = relationship("Device", lazy="selectin")

    __table_args__ = (
        Index("ix_device_loghost_device", "device_id"),
        Index("ix_device_loghost_status", "status"),
    )


class DeviceLogSetting(Base):
    """设备日志中心全局设置（单行）。

    ``receiver_enabled`` 是**页面上的接收开关**（运行时可变），与部署级总闸
    ``.env`` 的 ``SYSLOG_UDP_ENABLED`` 取「与」关系：总闸关闭时页面开关不可用。

    为什么单独存表、而不是只读 .env：运维需要**不重启平台**就停掉 UDP 接收。
    最常见的两种场景是 —— 514 端口被第三方程序占用、或平台自身要临时让出该端口；
    改 .env 必须重启进程，做不到「随手关、随手开」。

    关闭开关**只停接收，不动存量数据**：已留存的日志照常查询/导出/统计。
    等保要求网络日志留存 ≥ 6 个月，不能因为关一下开关就断档。
    """

    __tablename__ = "device_log_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    receiver_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 最后一次操作人（审计线索；完整操作留痕另见 audit_logs）
    operator: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
