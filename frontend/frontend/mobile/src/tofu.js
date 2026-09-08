// TOFU（Trust On First Use）连接管理
// 原生侧 AiopConnector 插件按 host:port 持久化"首次确认的服务器证书指纹"；
// 本模块提供前端封装：首次连接/指纹变更的确认分发、已信任平台管理。
import { Capacitor, registerPlugin } from '@capacitor/core'

export const isNative = Capacitor.isNativePlatform()

let _native = null
if (isNative) {
  try {
    _native = registerPlugin('AiopConnector')
  } catch (e) {
    _native = null
  }
}

/** 原生 AiopConnector 插件代理（web 环境为 null） */
export function nativeConnector() {
  return _native
}

// ---------- 全局对话框注册（由 TofuDialog.vue 提供） ----------
let _confirmHandler = null
let _manageHandler = null

export function registerTofuHandlers(handlers) {
  _confirmHandler = handlers.confirm || null
  _manageHandler = handlers.manage || null
}
export function unregisterTofuHandlers() {
  _confirmHandler = null
  _manageHandler = null
}

/**
 * 处理 TOFU 事件（api.js 响应拦截器调用）：
 * 弹确认框 → 用户信任则 pin 并返回 true（调用方重试请求），否则 false。
 * @param {{code:string, host:string, port:number, fingerprint:string, pinnedFingerprint?:string}} info
 */
export async function resolveTofu(info) {
  if (!_confirmHandler) return false
  try {
    return await _confirmHandler(info)
  } catch (e) {
    return false
  }
}

/** 打开"已信任平台"管理面板 */
export function openTofuManage() {
  if (_manageHandler) _manageHandler()
}

// ---------- 原生调用封装 ----------

async function callNative(method, args) {
  if (!_native) {
    const err = new Error('当前环境不支持原生 TOFU 通道')
    err.tofu = { code: 'NO_NATIVE' }
    throw err
  }
  return await _native[method](args || {})
}

/** 直接探测某服务器证书指纹（不持久化） */
export async function probeServer(host, port) {
  const r = await callNative('probe', { host, port })
  if (!r || r.ok === false) {
    const err = new Error((r && r.message) || '探测失败')
    err.tofu = { code: (r && r.code) || 'NETWORK_ERROR' }
    throw err
  }
  return r
}

/** 持久化信任某服务器证书指纹 */
export async function pinServer(host, port, fingerprint) {
  await callNative('pin', { host, port, fingerprint })
}

/** 移除已信任记录 */
export async function unpinServer(host, port) {
  await callNative('unpin', { host, port })
}

/** 已信任列表：[{host, fingerprint}] */
export async function listPins() {
  const r = await callNative('listPins', {})
  return (r && r.pins) || []
}

/** 原生通用 HTTPS 请求（axios adapter 使用） */
export async function nativeRequest(args) {
  const r = await callNative('request', args)
  if (!r) throw new Error('原生请求无响应')
  return r
}
