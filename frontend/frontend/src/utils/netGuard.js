/**
 * 全局 fetch 包装：统一超时 + 401 跳登录（覆盖各页面直接使用的原生 fetch）。
 *
 * ── 为什么需要按路径区分超时（2026-09-14 修）──
 * 原先一律 15s 硬超时，但「设备类操作」要真实 SSH/Telnet 登进网络设备，
 * 耗时远超 15s：
 *   - 单台配置备份：后端 BACKUP_TIMEOUT = 300s（设备慢时真会跑到几分钟）
 *   - 备份全部设备：后端是串行 for 循环，生产 90 台量级 → 30 分钟以上
 *   - 等保合规核查：逐台 SSH 采集运行态命令
 *   - 业务监控终端探测：串行 probe 全部终端
 * 结果：前端 15s 就 abort，用户看到「请求失败: signal is aborted without reason」，
 * 而后端其实还在继续跑（可能最终成功）—— 误报失败。
 *
 * 策略：普通查询保持 15s 硬超时（避免异常请求长期挂起），
 * 长耗时接口按路径白名单放宽；调用方也可用 init.timeout 显式覆盖。
 */

export const DEFAULT_TIMEOUT = 15000

/** [路径正则, 超时ms, 说明] —— 新增长耗时接口时补在这里 */
export const LONG_OP_TIMEOUTS = [
  [
    /^\/api\/v1\/config-backups\/manual-all$/,
    60 * 60 * 1000,
    '配置备份·全部设备（后端串行 N 台）',
  ],
  [
    /^\/api\/v1\/config-backups\/manual\//,
    10 * 60 * 1000,
    '配置备份·单台设备（后端上限 300s）',
  ],
  [
    /^\/api\/v1\/compliance\/check$/,
    10 * 60 * 1000,
    '等保合规核查（逐台 SSH 采集）',
  ],
  [
    // 匹配 /terminals/probe-all 与 /terminals/{id}/probe；
    // 不能用 /terminals/probe 前缀（漏掉带 id 的单台探测），
    // 也不能放宽成 /terminals/（会把列表、删除等快接口一起放宽）
    /^\/api\/v1\/business-monitor\/terminals\/(?:[^/]+\/)?probe/,
    10 * 60 * 1000,
    '业务监控终端探测（单个/串行批量）',
  ],
]

/**
 * 解析该请求应使用的超时（ms）。
 * 优先级：init.timeout 显式指定 > 路径白名单 > DEFAULT_TIMEOUT
 */
export function resolveTimeout(input, init, origin) {
  const custom = init && init.timeout
  if (typeof custom === 'number' && custom > 0) return custom

  const raw = typeof input === 'string' ? input : (input && input.url) || ''
  let path = raw
  try {
    path = new URL(raw, origin || 'http://localhost').pathname
  } catch {
    // 非常规 URL：保持原样参与匹配
  }
  for (const [re, ms] of LONG_OP_TIMEOUTS) {
    if (re.test(path)) return ms
  }
  return DEFAULT_TIMEOUT
}

/**
 * 安装 fetch 守卫。传入 window-like 对象以便测试。
 * @returns 包装后的 fetch
 */
export function installFetchGuard(win) {
  const origFetch = win.fetch.bind(win)

  win.fetch = async (input, init) => {
    // timeout 是本包装自定义字段，不传给原生 fetch
    const { timeout: _customTimeout, ...rest } = init || {}
    const controller = new win.AbortController()

    // 尊重调用方自带的 signal：任一方向 abort 都生效
    const callerSignal = rest.signal
    if (callerSignal) {
      if (callerSignal.aborted) {
        controller.abort(callerSignal.reason)
      } else {
        callerSignal.addEventListener('abort', () => controller.abort(callerSignal.reason), { once: true })
      }
    }

    const origin = win.location && win.location.origin
    const timeout = resolveTimeout(input, init, origin)
    let timedOut = false
    const timer = win.setTimeout(() => {
      timedOut = true
      controller.abort()
    }, timeout)

    try {
      const res = await origFetch(input, { ...rest, signal: controller.signal })
      const pathname = (win.location && win.location.pathname) || ''
      if (res.status === 401 && !pathname.startsWith('/login')) {
        // 会话失效：跳转登录页（避免重复跳转）
        if (!win.__loginRedirecting) {
          win.__loginRedirecting = true
          win.location.href = '/login'
        }
      }
      return res
    } catch (e) {
      // 把浏览器含糊的 AbortError 换成可读提示（并说明后端可能仍在执行）
      if (timedOut && e && e.name === 'AbortError') {
        const err = new Error(
          `请求超时（超过 ${Math.round(timeout / 1000)} 秒）；该操作可能仍在后台执行，请稍后刷新查看结果`
        )
        err.name = 'TimeoutError'
        err.cause = e
        throw err
      }
      throw e
    } finally {
      win.clearTimeout(timer)
    }
  }

  return win.fetch
}
