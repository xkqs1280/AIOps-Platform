"""NTP 授时功能模型（P1）。

三张表：
  - ``ntp_settings``：NTP 功能全局设置（单行）。
  - ``ntp_device_configs``：设备侧 NTP 配置的下发记录（对照 ``device_loghost_configs``）。
  - ``ntp_offset_records``：平台与上游时间源的偏差采样（用于大屏曲线与告警取数）。

设计要点（为什么要有这个功能）：
  网络设备的时钟不准会造成两个实际麻烦 ——
  ① 设备日志的时间戳不可信：排查故障时"日志时间"与"事件真实时间"对不上，
     多台设备之间的日志无法按时间轴拼成一条因果链；
  ② 部分认证/票据类协议（如证书校验、审计合规）要求设备时钟与基准时间的偏差在
     容忍范围内，超差会直接导致业务中断。
  平台本身**不自动调整自己的时钟**（改宿主时钟是危险动作，且容器内往往无权限），
  只做两件事：对外**授时**（应答设备的 NTP 请求），对内**度量**（显示与上游的偏差，
  管理员可手动触发一次同步）。

时间字段说明：
  - 全部时间字段按 ``DateTime(timezone=True)`` 存储（UTC）；
  - NTP 协议内部使用「自 1900-01-01 起的秒数」，与 Unix 时间差 2208988800 秒，
    换算常量定义在 ``app.services.ntp_service``（模型层不碰协议细节）。
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
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class NtpSetting(Base):
    """NTP 授时功能全局设置（单行）。

    两个开关刻意分开，因为它们是**两条独立链路**，故障面与风险完全不同：

    - ``upstream_enabled``：**上行取时**。平台主动去向上游时间源（默认阿里云
      ``ntp.aliyun.com``）请求时间，用于计算并展示「平台与云时钟的偏差」。
      只读操作、无侵入，但会周期性产生外网流量。
    - ``server_enabled``：**下行授时**。平台在本机监听 UDP 123，应答设备的 NTP 请求。
      开启后占用 123 端口（Linux 下 <1024 需要 CAP_NET_BIND_SERVICE），
      并且一旦设备配了这台平台做 NTP 服务器，设备时钟就依赖平台。

    两个开关**默认都关闭**：授时是"按需开启"的能力，不是部署即生效的默认行为。
    客户现场多数情况只当监控平台用，不需要它承担时间源角色；默认开启意味着
    凭空占一个特权端口、凭空对外发请求，不合理。

    ``upstream_server`` 允许留空 —— 留空时由服务层回退到配置项
    ``settings.NTP_UPSTREAM_SERVER`` 的默认值（阿里云），避免把默认地址固化成历史数据。

    ``listen_host`` 默认为 ``0.0.0.0``：设备的请求源地址无法预先枚举，
    只监听某一网卡会导致别的网段设备授时失败。真实部署时若宿主多网卡，
    由页面提示使用者确认下发给设备的应该写哪个地址（见 ``ntp_service`` 的
    ``local_address_candidates()``）。
    """

    __tablename__ = "ntp_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 下行授时（平台作 NTP 服务器）——UDP 123 监听
    server_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 上行取时（平台作 NTP 客户端，读上游偏差）
    upstream_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 上游时间源地址，留空 = 用 .env 默认值（ntp.aliyun.com）
    upstream_server: Mapped[str | None] = mapped_column(String(128))
    # 本机监听地址，默认全网卡
    listen_host: Mapped[str] = mapped_column(String(64), default="0.0.0.0", nullable=False)
    # 告警阈值（秒）。设计上要求"不超过 60 秒"，故默认 60；此值供时钟偏差告警规则读取。
    offset_threshold_seconds: Mapped[float] = mapped_column(Float, default=60.0, nullable=False)
    # 最后一次操作人（审计线索；完整操作留痕另见 audit_logs）
    operator: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class NtpCommandTemplate(Base):
    """设备侧 NTP 下发的**命令模板**（按 厂商 × 动作 各一条）。

    为什么模板要可编辑、而且落库：

    命令集不是"一份标准"，而是随设备型号与软件版本漂移的 ——
      - H3C 的 MDC 形态要写 ``clock protocol ntp mdc 1``，普通盒子写 ``clock protocol ntp``；
      - 华为 VRP 没有 ``clock protocol``，启用靠 ``undo ntp-service disable``；
      - 现场还可能要求带 ``source`` 源接口、指定 ``priority``、或换成设备自己解析的域名。

    把模板写死在代码里，意味着每遇到一处现场差异就要改代码、重发包、重升级。
    模板落库后现场工程师在页面上自己改，改动本身留痕（``operator`` / ``updated_at``），
    出问题还能一键恢复默认。

    正文中支持的占位符（渲染时替换，见 ``ntp_config_service.render_template``）：
      - ``{address}``：本次下发的 NTP 服务器地址（**必填**，否则回验无从判定）；
      - ``{clock_protocol}``：按「时钟协议」选择渲染为 ``clock protocol ntp``
        或 ``clock protocol ntp mdc N``（华为恒为空 —— VRP 无此命令）；
      - ``{mdc_id}``：MDC 编号。

    ``save`` 类命令行由渲染函数按「下发后保存配置」勾选**自动增删**，
    因此模板里写不写 ``save force`` 都行，勾选框始终有效。

    **无记录 = 用代码内置默认模板**（``ntp_config_service.DEFAULT_TEMPLATES``），
    所以不需要在部署时预置数据。
    """

    __tablename__ = "ntp_command_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # h3c / huawei
    vendor: Mapped[str] = mapped_column(String(16), nullable=False)
    # apply（下发）/ rollback（回滚）
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    # 模板正文（多行文本，含占位符）
    body: Mapped[str] = mapped_column(Text, nullable=False)
    operator: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("vendor", "action", name="uq_ntp_template_vendor_action"),
    )


class NtpDeviceConfig(Base):
    """设备侧 NTP 配置的下发记录。

    每台设备可有多行历史记录（按 ``applied_at`` 倒序取最新为当前生效配置），
    用于：① 审计"谁在什么时候改了什么"；② 回滚时还原下发前的 NTP 配置快照。

    ``device_id`` **可空**：设备从平台删除时只置 NULL、不删记录（变更留痕同样是审计
    证据）。因此 ``device_name`` / ``device_ip`` 在写入时冗余保存一份快照——否则设备
    删掉之后就再也说不清"这条记录到底改的是哪台设备"。
    """

    __tablename__ = "ntp_device_configs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("devices.id"), nullable=True
    )
    # 写入时的设备名/IP 快照（设备被删除后仍可辨认）
    device_name: Mapped[str | None] = mapped_column(String(128))
    device_ip: Mapped[str | None] = mapped_column(String(45))
    # 下发给设备的「NTP 服务器地址」= 平台本机对设备可达的地址
    server_address: Mapped[str | None] = mapped_column(String(64))
    # 生效形态：system（全局时钟协议）/ mdc（多 MDC 场景，需带 mdc N 参数）
    clock_mode: Mapped[str | None] = mapped_column(String(16), default="system")
    # applied / rolled_back / unverified / failed
    #   unverified = 命令已下发且无报错，但配置回读不可信，无法确认是否生效。
    #   单独成一档而不是并进 failed：读不到 ≠ 没配上，混为一谈会让操作人重复下发。
    status: Mapped[str | None] = mapped_column(String(16), default="applied")
    # 下发前后的 NTP 相关配置快照（用于回滚与差异比对）
    before_config: Mapped[str | None] = mapped_column(Text)
    after_config: Mapped[str | None] = mapped_column(Text)
    # 实际下发的命令（逐行，便于审计）
    commands: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    operator: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    device = relationship("Device", lazy="selectin")

    __table_args__ = (
        Index("ix_ntp_device_config_device", "device_id"),
        Index("ix_ntp_device_config_status", "status"),
    )


class NtpOffsetRecord(Base):
    """平台与上游时间源的偏差采样记录。

    为什么要独立建表而不是只在内存里存最新值：
    - 大屏要画偏差趋势曲线（内存里只有一个点画不出来）；
    - 时钟偏差告警需要"持续 N 分钟超阈值才告警"，必须有历史序列才能判定持续性；
    - 出问题后要能回溯"什么时候开始漂的、漂了多少"，这是排障关键证据。

    ``source`` 区分采样来源：``upstream``（主动向上游取时）/ ``manual``（管理员手动同步）。
    手动同步后补记一条，便于在大屏上标出"这里做过一次校准"。

    记录量按采样周期累积（默认 5 分钟一次 → 一天 288 条），量很小，
    保留期由 ``retention_service`` 统一处理。
    """

    __tablename__ = "ntp_offset_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 偏移量（秒）。符号按 RFC 5905 的 offset 定义 = 「上游时间 - 平台时间」：
    #   正值 = 上游快于平台 → 平台时钟**慢**了（需要往前调）
    #   负值 = 上游慢于平台 → 平台时钟**快**了（需要往回调）
    # 展示与告警一律取绝对值（关注偏差幅度，不关心快慢方向）。
    offset_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    # 采样往返时延（秒），用于判断该样本可信度：RTT 越大，偏移测量越不准
    round_trip_seconds: Mapped[float | None] = mapped_column(Float)
    # 上游时间源地址（快照；换源后仍能分辨历史样本是谁给的）
    source_address: Mapped[str | None] = mapped_column(String(128))
    # upstream / manual
    source: Mapped[str | None] = mapped_column(String(16), default="upstream")
    # 该次请求是否成功；失败时 offset 记 0 且 error 有值
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    sampled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_ntp_offset_sampled", "sampled_at"),
    )
