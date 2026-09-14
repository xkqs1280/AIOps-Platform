# NETCONF 与 Telemetry 对平台 SNMP 的替代能力分析

> 分析对象：《H3C 交换机 NETCONF API 二次开发指南》《H3C 交换机 Telemetry 二次开发指南（Comware V7）》
> 分析时间：2026-09-14　分析范围：AIOps 平台当前 SNMP 使用面 + 测试环境设备实测

---

## 一、结论先行

**两个协议都不能整体"替代"SNMP，但各自能替掉一半，且互不重叠。**

| 平台 SNMP 职责 | Telemetry | NETCONF | 说明 |
|---|---|---|---|
| 周期指标采集（CPU/内存/温度/接口状态） | **强替代** | 部分（仍拉模式） | Telemetry 是唯一真正换代的选项 |
| 告警事件接收（Trap） | 部分 | 部分 | 两者都比 Trap 弱，建议保留 Trap |
| 拓扑发现（LLDP）/ 设备识别 | 部分 | 部分 | 用哪个都行，收益不明显 |
| 配置管理（备份/回滚/合规基线） | 不涉及 | **强替代** | SNMP 完全做不到，这是净增能力 |

**推荐路线：先上 NETCONF，Telemetry 列入中期。** 理由：

1. **NETCONF 补的是平台能力空白**——现在配置备份靠 SSH 抓 `display current-configuration` 纯文本，
   NETCONF 的 `get-config` 拿到的是结构化 XML，可直接做配置差异比对、精确回滚、合规基线核查。
2. **NETCONF 落地成本低**：复用现有 SSH 通道与设备凭据，Python 有成熟的 `ncclient` 库，
   H3C 官方文档给了完整示例代码。
3. **NETCONF 已实测可用**：测试环境 4 台可连的 H3C 设备全部为 Comware V7，`display netconf` 命令存在。
4. **Telemetry 门槛最高**：需要设备硬件+软件版本双重支持 gRPC，且需向 H3C 索取业务 Proto 文件；
   本环境实测 `display grpc` 全部报 Unrecognized（不支持）。

---

## 二、平台当前 SNMP 使用面（盘点）

| 用途 | 实现位置 | 关键参数 | SNMP 的角色 |
|---|---|---|---|
| 周期指标采集 | `services/metrics_collector.py` | `COLLECT_INTERVAL=60s`、`SNMP_TIMEOUT=5s`、信号量并发 | 主力，负载主要来源 |
| 接口状态告警 | `services/alert_rule_engine.py` | `IF_OPER_STATUS` / `IF_ADMIN_STATUS` / `IF_NAME` 轮询 | 拉模式 |
| Trap 告警接收 | `routers/traps.py`（983 行） | snmptrapd → HTTP 转发，华为/H3C MIB 树映射 | 推模式，事件来源 |
| 拓扑发现 | `services/discovery_service.py` | LLDP、`sysDescr` | 拉模式 |
| 配置备份 | `services/backup_service.py` | **SSH CLI**（`display current-configuration`，300s 超时） | 不用 SNMP |
| 设备巡检 | `services/h3c_inspection_service.py` | **SSH CLI**（display 命令组） | 不用 SNMP |
| 等保合规 | SSH 实采（`SEC-*`）+ SNMP | — | 混合 |

> 注意：平台的**配置类**操作（备份、巡检）本来就走的 SSH CLI，不走 SNMP。
> 这说明"用 NETCONF 改善配置运维"是一个**自然演进**，不是推倒重来。

---

## 三、两协议技术要点（来自文档）

### 3.1 NETCONF

| 维度 | 内容 |
|---|---|
| 传输 | SSH(830) / SOAP over HTTP(80) / HTTPS(832) / Telnet / Console |
| 数据模型 | XML，YANG 建模，请求以 `top` 元素为起点 |
| 支持操作 | get、get-config、**get-bulk**、**edit-config**、action、**CLI**、lock/unlock、rollback、save、load、create-subscription、validate |
| 不支持 | candidate 候选库、confirmed-commit、xpath、distinct-startup、URL |
| 私有扩展 | get-bulk 批量取数、CLI 透传、save-point 回滚点、索引替换、增量下发 |
| 事件通知 | `create-subscription`，三类：Syslog 事件、监控事件（按 XPath+间隔轮询）、模块上报事件 |
| 会话约束 | SOAP 单会话并发 ≤4，其他访问方式同一会话**不支持并发** |
| 加锁影响 | `lock` 后**所有**其他配置方式（CLI/SNMP）均无法配置设备 |
| 大数据量 | `get-bulk` + `count` 分批，以最后一条索引作为下批游标 |
| 客户端 | Python `ncclient`（官方给出 H3C 完整示例，`device_params={'name':'h3c'}`，默认端口 830） |

**关键限制（事件订阅）**：

- 订阅**只对当前连接生效**，连接断开自动取消；
- **不支持事件回放**；
- 已有订阅存在时，不能再订阅或变更当前订阅。

→ 这意味着 NETCONF 事件订阅**可靠性不如 Trap**（Trap 是设备侧主动单发，无长连接依赖）。

### 3.2 Telemetry（gRPC）

| 维度 | 内容 |
|---|---|
| 传输 | gRPC over HTTP/2，默认端口 **50051**，可选 TLS/PKI 双向认证 |
| 数据模型 | YANG 模型路径（如 `ifmgr/statistics`、`Device/CPUs`） |
| 采样类型 | 周期采样、事件触发采样、条件触发采样（仅 gNMI 模式） |
| 编码格式 | GPB（高效二进制，需业务 Proto）、JSON、JSON_IETF |
| 过滤能力 | 谓语过滤（`[ifindex="12"]`，`ifmgr/statistics` 最多 64 条）、列选择（最多 24 子节点）、depth 采样深度 |
| 对接模式 | **Dial-in**：设备作 server，采集器主动连；断连后设备自动取消订阅，**需采集器重新发起**，不支持自动恢复 |
| | **Dial-out**：设备作 client，主动推给采集器；**支持自动重连**，但重连期间数据丢失；目标组建议 ≤5 个 |
| 数据丢失场景 | 主备倒换、保存配置重启、Dial-out 重连期间 |
| 采样精度影响因素 | 采样实例数目（如 10 万条路由）、数据源自身最小周期、设备 CPU 负载 |
| 客户端 | 需 `protoc` 生成代码，支持 C++/GO/Python/JAVA |

**关键门槛**：

1. **业务 Proto 文件必须向 H3C 技术支持索取**——除非走 gNMI 模式（`gnmi.proto` 开源）或 Dial-out 二层模型（`grpc_dialout.proto`，JSON 编码，只需公共 proto）。
2. 设备侧需 `grpc enable` + 专用本地用户（`service-type https`、角色 `network-admin`）。
3. **设备必须支持 gRPC 特性**（型号 + 软件版本双重要求）。

---

## 四、测试环境实测结果

### 4.1 设备清单与协议支持（.108 挂载设备）

通过平台自身凭据 + asyncssh（沿用 `backup_service` 的算法白名单）逐台只读探测：

| 设备 IP | 型号 | Comware 版本 | 镜像 | NETCONF | gRPC |
|---|---|---|---|---|---|
| 192.168.124.66 | S6850 | 7.1.070 Alpha 7170 | `s6850-cmw710-*-t7064p15.bin` | **支持** | 不支持 |
| 192.168.124.67 | SecPath F1090 | 7.1.064 Alpha 7164 | `sim_f1000_fw-*-a6401.bin` | **支持** | 不支持 |
| 192.168.124.68 | MSR36-20 | 7.1.064 Release 0427P22 | `msr36-cmw710-*-r0424p22.bin` | **支持** | 不支持 |
| 192.168.124.63 | WX5540H-HCL | 7.1.064 Alpha 7165 | `wx5540hhcl-cmw710-*-a6429.bin` | **支持** | 不支持 |
| 192.168.124.62 | S6850 | — | — | 登录超时 | — |
| 192.168.124.69 | S6850 | — | — | 登录超时 | — |

探测命令与判定依据：

- `display netconf` → 返回 `% Incomplete command`（命令存在，需补参数）→ **支持 NETCONF**
- `display grpc` → 返回 `% Unrecognized command`（命令不存在）→ **不支持 gRPC/Telemetry**

### 4.2 两点必须注意的解读限制

1. **这批设备带有 `Alpha` 版本号与 `sim_` 前缀镜像，疑为仿真/模拟环境**，
   其能力**不能代表生产 172.29.191.x 的 90 台真实设备**。
   生产设备是否支持 Telemetry，**必须单独普查**。
2. 外部资料交叉验证：H3C 官网明确 S6850 系列"支持 GRPC…同时支持 Telemetry"，
   但社区资料显示 S6800/S6850 需 **CMW710-R6715P01 及以上**版本才完整支持 gRPC Telemetry。
   → 结论是**硬件可能支持、软件版本未必支持**，版本是决定性的。

### 4.3 顺带发现的一个落地坑

新版 paramiko 用默认算法**连不上**这批 H3C 设备，报
`IncompatiblePeer: no acceptable host key`。平台 `backup_service.py` 已通过显式放宽
`server_host_key_algs` / `kex_algs` / `mac_algs` 解决。

→ **netconf 用的 ncclient 同样基于 paramiko，接入时必须一并放宽算法白名单**，
否则会在握手阶段直接失败。

---

## 五、落地建议

### 5.1 优先级

| 阶段 | 动作 | 价值 | 风险 |
|---|---|---|---|
| **第一步** | NETCONF 只读试点：用 ncclient 对 1~2 台设备执行 `get-config`，与现有 SSH 备份内容做等价性比对 | 验证可行性，零风险 | 低 |
| **第二步** | 配置备份增加 NETCONF 通道：配置态数据以结构化 XML 存储，支持差异比对与精确回滚 | 补平台能力空白 | 低（新增通道，不动现有链路） |
| **第三步** | 合规基线核查接入 NETCONF：等保 `CFG-*` 规则直接对接 YANG 模型，替代文本正则匹配 | 提升规则准确性 | 中 |
| **第四步** | Telemetry 试点：**先做设备能力普查**，仅在支持 gRPC 的设备上开 Dial-out | 采集能力代际提升 | 高（需设备支持 + Proto 文件） |

### 5.2 设备能力普查方法（一条命令）

在每台设备执行：

```
display grpc
```

- 返回 gRPC 相关信息 → 支持，可纳入 Telemetry 试点
- 返回 `% Unrecognized command` → 不支持，需评估版本升级

建议同时对生产 90 台设备批量执行，形成能力清单后再定 Telemetry 覆盖面。

### 5.3 明确不建议做的事

- **不要下线 Trap 接收**：NETCONF 事件订阅"断连即失效、无回放"，可靠性不及 Trap；
  且平台现有 983 行 MIB 映射覆盖链路 down、堆叠、路由协议等，迁移成本高、收益低。
- **不要指望 NETCONF 替代指标采集**：它仍是拉模式，且 XML 报文开销比 SNMP PDU 更大，
  在大规模周期性采集场景下不占优势。
- **生产切换前必须在真实设备上验证**：当前实测结论来自仿真环境，不可直接外推。

---

## 六、一句话总结

> **"替代 SNMP"这个问法本身需要拆开看**：要替掉**采集负载**，答案是 Telemetry（但门槛高、需先普查设备）；
> 要打开**配置运维**这个 SNMP 根本做不到的领域，答案是 NETCONF（门槛低、本环境已验证）。
> 两者的正确关系是**分工互补**，而不是二选一去替代 SNMP。
