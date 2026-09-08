# 移动端 TOFU 方案（方案 B）实现说明与 iOS 兼容设计

> 版本：2026-09-08 · 关联代码：`mobile-app/android/.../AiopConnectorPlugin.java`、
> `frontend/frontend/mobile/src/{tofu.js,tofuAdapter.js,components/TofuDialog.vue}`、
> `views/Login.vue`、`App.vue`、`api.js`

## 1. 背景与目标

- 一个 APK 通用连接**任意客户自部署**的 AIOps 平台（各自独立自签 HTTPS、可能 SAN 缺 IP）。
- 终端用户**零系统级证书操作**（不引导装证书）。
- 已决策走 **TOFU（Trust On First Use）**：首次连接确认服务器证书指纹并持久化，之后指纹命中即信任、不符即拒（与 SSH 首次连接同一安全模型）。

## 2. 为什么必须落在原生网络层（关键约束）

```
APK 本地加载 SPA（capacitor://localhost）
   └─ 远程仅 API XHR（axios → https://ip:port/api/v1）
        └─ WebView 内 XHR 的证书校验 = Chromium/WebKit 网络栈（系统信任库）
        └─ WebViewClient.onReceivedSslError 只回调「主框架导航」，管不到 XHR → 无效
        └─ NSC 只能表达静态信任（system/user/内置 raw），无法表达"动态 per-host 指纹" → 不够
```

结论：**业务流量改走原生网络通道，在原生侧实现 per-host 指纹信任**。

## 3. Android 落地（已完成，随本次 APK）

| 文件 | 说明 |
|---|---|
| `AiopConnectorPlugin.java`（新增） | Capacitor 插件：`request / probe / pin / unpin / listPins` |
| `MainActivity.java` | `registerPlugin(AiopConnectorPlugin.class)` |
| `frontend/mobile/src/tofu.js`（新增） | 插件封装 + 全局确认/管理分发（web 环境安全降级） |
| `frontend/mobile/src/tofuAdapter.js`（新增） | axios 自定义 adapter：原生走 TOFU 通道，web 回退默认 XHR |
| `frontend/mobile/src/components/TofuDialog.vue`（新增） | 全局确认框（首连/指纹变更）+ 已信任平台管理 |
| `frontend/mobile/src/api.js` | 挂 adapter；TOFU 事件自动弹确认 → pin → 自动重试一次 |
| `frontend/mobile/src/views/Login.vue` | 移除"引导装证书"，替换为"已信任的平台管理"入口 |
| `App.vue` | 挂载全局 TofuDialog |

原生实现要点：
- **pin 存储**：`SharedPreferences("aiops_tofu_pins")`，key=`host:port`（host 小写），value=叶子证书 SHA-256（大写冒号分隔）。
- **request 流程**：`无 pin → 内部 TLS 探测取指纹 → 返回 TOFU_FIRST_USE{host,fingerprint,subject,notAfter}`（不发业务请求）；`有 pin → pinned TrustManager 建连`（指纹命中信任、不符抛错 → 捕获后探测当前指纹 → 返回 TOFU_MISMATCH{pinnedFingerprint, fingerprint}）；hostnameVerifier 恒真（指纹已锁定证书身份，SAN 缺 IP 不再拦截）。
- **probe**：trust-all SSLSocket 仅做握手取叶子证书（不发业务数据）。
- **multipart**：升级包上传由前端把 `<6MB` 文件转 base64，原生组 multipart 流式写入；>6MB 明确提示走电脑端执行平台升级（移动端升平台包低频）。
- **重定向**：`setInstanceFollowRedirects(false)`（避免跨 host 绕过 pin）。
- 其余 API（JSON、Bearer、FormData 上传小文件、超时透传）与 WebView 行为对齐；401 处理仍走 axios 原逻辑。

前端交互闭环（Login 自动弹确认）：
`登录 → 请求 → 原生无 pin → FIRST_USE → api 拦截器 → TofuDialog 展示指纹 → 用户「信任并继续」→ pin 落库 → 自动重试 → 成功进主界面`。
指纹变更时展示「已信任 vs 当前」双指纹 + 风险提示。

## 4. iOS 兼容设计（尚未实现，需 Mac + Xcode 环境）

### 4.1 推荐路线：iOS 端实现同名 AiopConnector 原生插件（与 Android 同构）

前端代码 100% 复用（`tofu.js / tofuAdapter.js / TofuDialog.vue / api.js / Login.vue` 均平台无关，仅通过 `Capacitor.isNativePlatform()` 与 `registerPlugin('AiopConnector')` 路由）。iOS 工程由 `npx cap add ios` 生成后，在 ios/App/App/ 下新增插件：

| iOS 文件 | 说明 |
|---|---|
| `AiopConnectorPlugin.swift`（新增） | Capacitor 8 插件：同名字方法 request/probe/pin/unpin/listPins |
| `AiopURLSessionDelegate`（新增） | `URLSession:didReceive:completionHandler` 处理 `serverTrust` challenge：取叶子证书 SHA-256 → 与 UserDefaults 中 `host:port` pin 比对 → 命中给 `URLCredential(trust:)`；无 pin 先拒并在内部探测后回 FIRST_USE |
| `AppDelegate.swift` | 注册插件 |
| `Info.plist` | `NSAppTransportSecurity` 保持默认（不全局放开，信任由 TOFU 逐 host 控制） |

要点与差异：
- 网络层用 `URLSession`（原生可靠回调 challenge，避免 WKWebView challenge 对 XHR 是否回调的不确定性）。
- pin 存 `UserDefaults`（key 同 Android 的 `host:port` 规范，便于两端行为一致）。
- multipart 上传：`URLSessionUploadTask` 从 base64（前端同款 <6MB 限制）构造 body。
- 错误码与 Android 插件保持一致（`TOFU_FIRST_USE / TOFU_MISMATCH / NETWORK_ERROR`），前端拦截器无需分支差异。

### 4.2 备选路线（若不想引入原生代理）

iOS 上 `WKWebView` 的 `WKNavigationDelegate.webView(_:didReceive:completionHandler:)` 对 `.serverTrust` challenge 做逐 host 指纹判断（同 Android 思路）。风险：不同 iOS 版本对「页面内 XHR 的 serverTrust 是否回调 delegate」行为不一致，需要真机矩阵验证；验证不过则回退 4.1。**建议直接走 4.1**。

### 4.3 打包与签名

- 需 Apple Developer 账号、证书与 provisioning profile；`cap sync ios` 后 Xcode archive 导出 ipa（或 TestFlight）。
- iOS 未纳入本次 Android APK 交付，等 Mac 环境后按本设计实现。

## 5. 安全模型与运维须知

- TOFU 首次连接**假设可信**（理论上存在首连劫持窗口）；此后伪冒服务器因指纹不符被拒。
- 平台服务器**更换证书/重装后**：终端需在「登录页 → 已信任的平台管理」删除旧记录，重新连接确认一次。
- 管理员首次交付时可提供服务器证书 SHA-256 指纹，与 APP 弹窗核对（probe 也展示 CN/有效期辅助核对）。
- `CaInstallerPlugin` 与 `res/raw/aiops_root_ca.crt` 保留为降级入口，不再作为主流程。

## 6. 验证矩阵（Android APK 交付后执行）

1. 连测试服 `192.168.124.108:8000` → 首连弹指纹确认 → 信任 → 登录成功。
2. 杀进程重进（不清数据）→ 直接登录成功（免确认）。
3. 连生产服 `172.29.191.2:8000`（不同自签证书）→ 再次弹确认（证明按 host 独立 pin）。
4. 伪冒验证：同一 host 换成另一证书（如改 hosts 或临时换平台证书）→ 报指纹不一致，拒绝连接。
5. 管理面板：删除记录后重新连接 → 回到首连确认流程。
6. 普通浏览器 Web 端（非 native）不受影响（adapter 回退默认 XHR）。
