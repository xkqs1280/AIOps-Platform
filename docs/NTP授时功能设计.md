# NTP 授时功能 · 设计定稿

> 决策：华哥 2026-09-23 拍板。状态：**设计已定，未开始实现**。

## 一、需求定稿（华哥四点决策）

| # | 决策项 | 华哥的选择 |
|---|---|---|
| 1 | 平台是否自动调整系统时钟 | **不自动调整**；显示与云时钟的误差值；提供**手动同步按钮**（管理员触发） |
| 2 | 下发命令模板 | **默认模板用 H3C**；模板可编辑；**不用 `update-calendar enable`** |
| 3 | 偏差采集 | **双路**（NTP 被动 + SSH 主动） |
| 4 | 告警阈值 | **偏差不超过 60 秒** |
| 5 | **功能总开关** | **默认关闭，需要时手动开启**（照 `device_log_settings` 的接收开关模式） |

## 一·补、总开关设计（华哥决策 5）

**默认关闭** —— 功能上线后**不自动对外提供授时服务**，必须管理员手动开启。

- 照抄 `device_log_settings` 的成熟模式：单行设置表 `ntp_settings`（`server_enabled / upstream_enabled /
  operator / updated_at`）+ `PUT` 开关 API（`admin_only`）+ `record_audit` 留痕。
- **运行时生效，不重启监听**：用 `asyncio.Event` 唤醒/挂起循环；关闭即 `transport.close()` 释放 UDP 123。
- **建议拆成两个独立开关**（比日志中心更细，因为上行/下行风险不同）：
  - `upstream_enabled`：平台向阿里云取时（**只读、零风险**，可默认开或与总开关联动）；
  - `server_enabled`：平台对外提供 NTP 授时（**对外监听端口**，默认关，需明确开启）。
- 绑定失败诊断照抄 syslog 的 `bind_error / bind_occupier / bind_hints` 三件套（含 Linux
  `CAP_NET_BIND_SERVICE` 提示与 Windows WinError 提示）。
- ⚠️ 与日志中心同理：**`.env` 总闸 AND 页面开关**双条件（避免只改一处以为生效）。

## 二、总体架构

两个方向的时间流：

```
阿里云 ntp.aliyun.com ──①上行同步──▶ AIOps 平台 ──②下行授时──▶ 纳管设备
                                        │
                              ③偏差采集（双路） → ④告警/大屏
```

| 层 | 新增 | 复用 |
|---|---|---|
| 上行取时 | `ntp_service.py::ntp_upstream_loop`（UDP 123 客户端，自实现） | `start_supervised` + `while True` + `sleep` |
| 下行授时 | `ntp_server_loop`（asyncio UDP 123 应答） | syslog 接收器同模式 + bind 诊断三件套 |
| 命令下发 | `ntp_device_service.py`（下发/回验/回滚） | 照抄 `device_loghost_service` |
| 偏差采集 | `clock_offset` → `MetricRecord` | 现成指标表 + 采集循环 |
| 告警 | **改 `alert_rule_engine.py`** | 去重/抑制/通知全复用 |
| 前端 | `NtpSettings.vue` + 大屏卡片 | `DisplaySettings.vue`、`Devices.vue` |

**依赖新增：零**（`httpx` 已在，NTP 报文手写 48 字节）。

## 三、上行同步（平台 ← 阿里云）

- 时间源：`ntp.aliyun.com`（公网、免费、IPv4），备用 `ntp1~7.aliyun.com`。
- 自建 UDP 123 客户端，**不引 ntplib** —— 需要 offset **和 RTT** 原始值，自己算更透明。
- **不自动调整系统时钟**（华哥决策 1）。只做两件事：
  1. 页面上显示「平台时间 vs 云时钟偏差 X ms」；
  2. 偏差超阈值 → 告警（提示运维介入）。
- **手动同步按钮**：管理员点击才真正同步平台时间。**这是系统级变更，必须做三重防护**：
  - `admin_only` 权限；
  - 走审计（`record_audit`）；
  - **二次确认**（前端弹窗 + 明确告知"会改变服务器系统时间"）。
  - ⚠️ 同步前**先记录原时间**，同步后记录新时间与偏差，落审计详情，便于追溯。
  - ⚠️ 注意会影响数据库事务时间戳、日志时间、JWT 过期判断 —— 步进量应该很小（正常环境偏差通常毫秒级）。

## 四、H3C 默认模板（重点：三个必配 + 一个互斥）

### ⚠️ 现场踩坑警示（务必写进模板注释）

1. **`ntp-service enable` 在 Comware V5 下默认关闭** —— 不开启，后面配置全部不生效。
2. **`clock protocol ntp` 极易漏** —— 漏了它，现象是"配置全对但时间不同步"。**这是最高频的坑**。
3. **`ntp-service update-calendar enable`** —— 同步后写硬件 RTC，断电不丢时间。
4. **NTP 与 SNTP 互斥** —— 若设备开了 `sntp enable`，NTP 起不来。
   ⚠️ **但不要盲目在模板里加 `undo sntp enable`** —— 有些设备 SNTP 正被业务使用，undo 是副作用。
   正确做法：**回验失败时在诊断提示里点名"检查是否启用了 SNTP"**，由管理员决策，不自动改。

### 默认模板（H3C Comware V5）— 已按华哥 2026-09-23 提供的版本对齐

```
system-view
clock timezone Beijing add 08:00:00
clock protocol ntp
ntp-service enable
ntp-service unicast-server {{server_ip}}
# ntp-service source-interface {{source_interface}}      ← 注释掉，需要时打开
return
save force
```

> 华哥提供的原版（三行核心）：
> ```
> system-view
> [H3C] clock protocol ntp                 // 将时间获取方式设置为NTP
> [H3C] ntp-service enable                 // 开启NTP服务
> [H3C] ntp-service unicast-server 192.168.1.100   // 指定NTP服务器IP
> ```
> 方向正确、两条关键命令都在。模板在其基础上补 2 处**下发必需**的部分（见下）。

**相对华哥原版的 3 处补充（前两处是下发场景的硬要求，第三处见下节）**：

| 补充 | 原因 |
|---|---|
| **`return` + `save force`** | ① `display current-configuration` 改的只是**运行配置**，不 save 则**重启全丢**；② `save` 需在用户视图执行，缺 `return` 不够稳。参照 loghost 下发的做法（`save force`）。 |
| **`source-interface` 改为注释可选** | 多网段/多网卡环境下，不指定源接口可能**从错误 IP 出去、平台收不到回包**。默认保持简单，现场需要时打开。 |
| **`clock timezone Beijing add 08:00:00`（2026-10-08 加）** | 设备时区默认 UTC → 校了时钟仍差 8 小时，见下节。 |

> ⚠️ **华哥 2026-09-23 决策：模板中不要 `ntp-service update-calendar enable`。**
> 该命令的作用是同步后写硬件 RTC（断电不丢时间），但华哥明确要求去掉 —— **不替现场决定 RTC 行为**。
> 现场若需要，由模板编辑功能自行添加。

**`clock timezone Beijing add 08:00:00`（设时区）—— 2026-10-08 华哥决定纳入默认下发，与先前设计相反**

- 先前的判断是"各客户时区要求不同 → 做成注释可选项"。**现场推翻了这个判断**：
  H3C 出厂默认时区是 **UTC**，NTP 只同步 UTC 时刻、不带时区信息 ——
  不置时区的话系统时钟校准了，`display clock` 仍显示 UTC，**比北京时间差 8 小时**，
  看上去像"授时没生效"。
- 现在**默认下发北京时间**，且放在 `clock protocol ntp` **之前**（先定时区、再启用 NTP 客户端）。
- 用**固定北京时间**而不是占位符：模板是"所见即所发"，页面上要能原样看到这条命令；
  跨时区客户在页面模板里自行改（模板可编辑 + 可恢复默认）。
- **回滚不动时区** —— 它不是本次下发的副作用，撤销会改变设备原有显示口径。
- ⚠️ 华为的对应命令是 `clock timezone BJ add 08:00:00`（同名但**时区名不能带空格**），
  且 VRP 的时钟形态由 `clock protocol` / `ntp-service` 决定 —— **华为是否需要配套时区命令尚未真机验证，
  暂不加入华为模板**。

### 默认模板（华为 VRP）

```
system-view
ntp-service enable
ntp-service unicast-server {{server_ip}}
return
save
```

- `{{server_ip}}`：平台地址，前端下拉由 **`device_loghost_service.local_address_candidates()`** 枚举（现成函数，直接复用）。
- `{{source_interface}}`：源接口，**模板中默认注释掉**，现场按需打开（如 `Vlan-interface 1`）。
- **Comware V7 差异**：`source-interface` 关键字变为 `source interface-type interface-number`（V7 手册语法），模板编辑功能正好用来应对这种版本差异。
- **默认模板已由华哥 2026-09-23 确认方向**（H3C 三行核心），平台内置时以「华哥原版 + 上述 3 处下发必需补充」为准。
- 华为作为对照内置，**默认选中 H3C**（华哥决策 2）。

### 回验命令

```
display ntp-service status      # 看时钟状态 / 是否同步 / 参考源
display clock                   # 看当前时钟
display ntp-service sessions    # 看会话状态（H3C）
```

## 五、下发流程（照抄 device_loghost_service）

```
预览命令 → open_device_session → 快照 → 逐条下发 → 再快照 → 回验判定 → 落库留痕
```

- 复用 `DeviceSession.run()` 执行命令、`local_address_candidates()` 取地址。
- 回验四态沿用：`applied / rolled_back / unverified / failed`。
- **`unverified` 务必照抄**：专门处理"命令下发无报错、但回读不可信"（空回读不可信，区分"真没配"与"型号不支持过滤语法"），避免误判。
- 批量：照 `apply_loghost_to_devices` —— `APPLY_CONCURRENCY=4`、`_run_bounded` 保证每台恰一条结果、单台异常不影响整批。
- 表：`ntp_device_configs`（对照 `device_loghost_configs`）。

## 六、偏差采集（双路，华哥决策 3）

| 方式 | 原理 | 数据源 | 特点 |
|---|---|---|---|
| **被动**（主力） | 设备发 NTP 请求时，报文含客户端发送时间戳 → 反推设备时钟算 offset | NTP 报文 | 零额外开销；仅在设备真在用平台授时时有效 |
| **主动**（补充） | 定期 SSH 跑 `display clock` 与平台时间比对 | SSH | 能覆盖"没配 NTP"的设备，发现压根没同步的情况 |

- 两路结果都写同一个 `clock_offset` 指标（单位 ms），落 `MetricRecord`。
- 被动路**顺带记录 RTT**（链路质量，用于判断偏差可信度）。

## 七、时钟一致性告警

🔴 **必须改代码，加 DB 规则不会生效**（告警引擎半硬编码）。改动四处：

1. `alert_rule_engine.py` 的 `SUPPORTED_METRICS` 加 `clock_offset`；
2. 新增 `_eval_clock_offset()`；
3. `_evaluate_device` 里加分派；
4. 种子一条默认 `AlertRule`（阈值可由管理页调整）。

其余全复用：`_ensure_alert` 去重、`alert_suppressor` 抖动/风暴抑制、`dispatch_alert` 通知。

### 阈值（华哥决策 4：不超过 60 秒）

| 级别 | 阈值 | 含义 |
|---|---|---|
| info | > 1s | 开始漂移 |
| warning | > 10s | 日志关联会出错 |
| **major** | **> 60s** | **超等保红线，必须处理**（华哥定的上限） |
| critical | 无授时记录 | 设备可能压根没配 NTP |

## 八、附带收益：解决日志中心「8 小时偏差」

日志中心的设备时钟/时区偏差，现在靠 `SYSLOG_DEVICE_TZ_OFFSET_HOURS` **手工补偿**（默认 0，因实测设备出厂为 UTC）。
设备走 NTP 对时后，时区与时间规整，该补偿参数可退为"兼容老设备"角色。**两功能天然配套。**

## 九、前置验证（做之前必须实测，不通过则方案要改）

### ✅ 已验证（2026-09-23，在 .108 实测）

| 项 | 结果 | 证据 |
|---|---|---|
| **上行 UDP 123**（平台→阿里云） | ✅ **通** | `ntp.aliyun.com` / `ntp1.aliyun.com` 均可连；.108 上 chrony 正在同步 |
| **权限：能否绑 123 特权端口** | ✅ **可，且无需改 systemd** | unit 已有 `User=admin1` + **`AmbientCapabilities=CAP_NET_BIND_SERVICE`**（514 正是靠它监听）；`sudo` 下 `bind('0.0.0.0',123)` 成功 |
| **123 端口占用** | ✅ 空闲 | `ss -lunp` 无 `:123` 监听 |
| **平台侧全链路（监听→收包→回包→客户端收到）** | ✅ **通** | 自测：`SERVER_BOUND` → `SERVER_GOT_REQ len 48` → `SERVER_SENT_REPLY` → `CLIENT_OK len 56` |

### ✅ 真机实测：设备成功同步（2026-09-23，决定性成功）

**HX-SW1（`192.168.124.62`，华为 S6850）实测输出**：
```
Clock status: synchronized              ← 成功！
Clock stratum: 3                        ← 平台 stratum=2 + 1，层级链正确
System peer: 192.168.124.108
Local mode: client
Reference clock ID: 192.168.124.108
Root delay: 6.21033 ms
Root dispersion: 76.75171 ms
Reference time: Wed, Sep 23 2026  8:53:39.867
```

🔴 **核心结论：应答报文必须"字段自洽"，否则设备收到也拒绝采纳。**
H3C 手册 1.5.1 明确：「**服务器需要通过与其他设备同步或配置本地时钟作为参考时钟等方式，
使得自己的时钟处于同步状态，否则客户端不会将自己的时间与服务器的时间同步。**」

**实测对比：不合规桩 vs 合规桩**

| 字段 | 不合规桩（被拒） | 合规桩（成功） | 说明 |
|---|---|---|---|
| stratum | **1**（硬写） | **2** | 填 1 等于自称顶级时钟，不可信；填 2 如实声明"次级" |
| root delay | **0** | **4ms** | 全零 ⇒ 声明"无上游"，矛盾 |
| root dispersion | **0** | **10ms** | 同上 |
| reference time | **全零**（`00000000.00000000`） | **真实当前时间** | **最关键**：全零 ⇒ 设备判定服务器未同步 |
| reference id | 无 | `AIOP` | 平台标识 |

- 不合规桩的现象：设备**收到了应答**（平台侧日志有记录）、但 `Clock status: unsynchronized`、
  `reference=INIT`、`reach=0`、`Reference time: 00000000.00000000  Thu, Feb 7 2036`（全零的经典显示）。
- 合规桩的现象：**`synchronized`**，一切正常。
- ⚠️ 设备侧显示的 `root dispersion 76.75ms` 比平台填的 10ms 大 —— 这是**设备侧自身累积**
  （抖动/精度损耗计入），**属正常现象**，非 bug。但客户若对精度有要求，此值决定实际误差量级。

### ⚠️ 两个实测发现（影响设计，务必注意）

1. 🔴 **平台上已有 chrony 在运行，且已占用系统时间同步角色。**
   .108 实测：`NTP service: active`、`System clock synchronized: yes`、当前源是
   `ntp-nts-*.ps5.canonical.com`（Ubuntu 默认），偏差约 **2~15ms**。
   ⇒ **设计含义**：
   - 平台自身对时**已由操作系统解决**，我们**不该重复实现**"平台去同步上游"这件事（那是我原方案里的多余部分）；
   - 平台功能应聚焦**两件事**：① 把"平台当前偏差"**读出来展示**（chrony 可查），② **对外提供 NTP 授时**（设备→平台）；
   - 若客户要改用阿里云源，属于**改系统 chrony 配置**，不是我们平台的功能。
   - ⚠️ 但**生产（Windows）没有 chrony**，那里才需要我们自己做客户端取时 —— 两平台策略不同，实现时要分支。

2. 🔴 **`.108` 有多张网卡**：`192.168.124.108`（ens33）、**`192.168.11.100`（ens37）**、
   外加 docker 的 `172.17.0.1` / `172.18.0.1`。
   ⇒ **NTP 服务器地址必须显式选择**，绑 `0.0.0.0` 会把 docker 网段也暴露。给设备下发时应选
   **设备可达的那张网卡 IP**（此处应为 `192.168.124.108`）。这印证了设计里"监听地址可配置 + 地址下拉"的必要性。

### ⏳ 已通过：设备 → 平台 UDP 123（2026-09-23 实测成功）

- **HX-SW1（.62）实测 `synchronized`**，见上节。
- **平台侧抓到两台设备的请求**：`192.168.124.63`、`192.168.124.62`（均 `mode=3`、48 字节标准包）
  ⇒ **`.63` 那台早就配了指向平台的 NTP**，只是一直没人应答。说明现场已有真实需求。
- ⚠️ **MDC 设备变体**：某台设备（提示符 `AC`）是 MDC 架构，`clock protocol ntp` 报
  `% Incomplete command`，**必须写 `clock protocol ntp mdc <ID>`**。
  ⇒ 模板**必须支持多变体**（普通版 / MDC 版），或按设备型号自动选。
  ⇒ 现有合规规则 `CFG-005` 只认 `ntp-service unicast-server|enable`，**对 MDC 设备可能误判**，实现时处理。

**建议顺序**：先在 .108 把 UDP 123 双向打通，再动代码。**（已完成 ✅）**

## 十、实现顺序

1. 上行取时循环 + 偏差显示
2. 下行 UDP 123 应答
3. 命令下发（H3C/华为模板 + 回验回滚）
4. 告警引擎扩展（clock_offset）
5. 前端设置页 + 大屏卡片
6. 手动同步按钮（含审计与二次确认）

## 十一、其他风险

- **平台是所有设备的时间源** ⇒ 平台自身偏差监控是**必需项**（否则全网跟着错）。
- **UDP 123 是新增对外监听端口** ⇒ 按变更管理走；建议配合 v4.5.7 的 IP 白名单思路考虑限定来源网段。
- **生产环境无日志** ⇒ 绑定/同步状态必须前端可见，不能只打日志。
