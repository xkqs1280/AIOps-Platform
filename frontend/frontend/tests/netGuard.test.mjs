/**
 * netGuard 回归测试（node --test 无关，直接 node 运行）
 *
 * 核心回归用例：配置备份这类长耗时请求，在 15 秒后**不再**被 abort。
 * 用可控时钟让「20 秒」瞬间完成，无需真的等待。
 *
 * 运行：node tests/netGuard.test.mjs
 */
import assert from 'node:assert/strict'
import {
  installFetchGuard,
  resolveTimeout,
  DEFAULT_TIMEOUT,
  LONG_OP_TIMEOUTS,
} from '../src/utils/netGuard.js'

// ── 可控时钟：让 setTimeout 手动推进 ──
function makeClock() {
  let timers = []
  let now = 0
  return {
    setTimeout(fn, ms) {
      const t = { fn, at: now + ms }
      timers.push(t)
      return t
    },
    clearTimeout(t) {
      timers = timers.filter((x) => x !== t)
    },
    advance(ms) {
      now += ms
      // 按到期时间顺序触发（同一时刻按注册先后）
      const due = timers.filter((t) => t.at <= now).sort((a, b) => a.at - b.at)
      timers = timers.filter((t) => t.at > now)
      for (const t of due) t.fn()
    },
    get pending() {
      return timers.length
    },
  }
}

/** 构造 window-like 对象；handler 决定 fetch 行为 */
function makeWin({ handler, pathname = '/devices' }) {
  const clock = makeClock()
  const win = {
    AbortController,
    setTimeout: (fn, ms) => clock.setTimeout(fn, ms),
    clearTimeout: (t) => clock.clearTimeout(t),
    location: { origin: 'https://aiops.example', pathname, href: '' },
    __loginRedirecting: false,
  }
  win.fetch = handler(clock)
  win.__clock = clock
  return win
}

/** 模拟「需要 ms 才返回」的原生 fetch，并对 signal 做出 abort 反应 */
const slowFetch =
  (ms, status = 200) =>
  (clock) =>
  (input, init) =>
    new Promise((resolve, reject) => {
      // 复刻浏览器原生报错文案
      const abortErr = () => {
        const e = new Error('signal is aborted without reason')
        e.name = 'AbortError'
        reject(e)
      }
      const sig = init && init.signal
      // 原生 fetch 对「已 aborted 的 signal」会立即拒绝
      if (sig && sig.aborted) return abortErr()
      const t = clock.setTimeout(() => resolve({ status, url: String(input) }), ms)
      if (sig) {
        sig.addEventListener(
          'abort',
          () => {
            clock.clearTimeout(t)
            abortErr()
          },
          { once: true }
        )
      }
    })

// ── 极简测试运行器 ──
const tests = []
const test = (name, fn) => tests.push({ name, fn })

// ═══ 1. 超时解析（纯函数）═══
test('普通查询保持 15s 硬超时', () => {
  assert.equal(resolveTimeout('/api/v1/devices', undefined, 'https://x'), 15000)
  assert.equal(resolveTimeout('/api/v1/alerts', undefined, 'https://x'), 15000)
  assert.equal(resolveTimeout('/api/v1/dashboard/summary', undefined, 'https://x'), 15000)
})

test('配置备份·单台放宽到 10 分钟（后端上限 300s）', () => {
  assert.equal(resolveTimeout('/api/v1/config-backups/manual/5', undefined, 'https://x'), 600000)
})

test('配置备份·全部设备放宽到 60 分钟（后端串行 N 台）', () => {
  assert.equal(resolveTimeout('/api/v1/config-backups/manual-all', undefined, 'https://x'), 3600000)
})

test('等保合规核查放宽（逐台 SSH 采集）', () => {
  assert.equal(resolveTimeout('/api/v1/compliance/check', undefined, 'https://x'), 600000)
})

test('终端探测（单个与批量）都放宽', () => {
  assert.equal(resolveTimeout('/api/v1/business-monitor/terminals/probe-all', undefined, 'https://x'), 600000)
  assert.equal(resolveTimeout('/api/v1/business-monitor/terminals/12/probe', undefined, 'https://x'), 600000)
})

test('带 query / 绝对 URL / Request 对象均能正确匹配', () => {
  assert.equal(resolveTimeout('/api/v1/compliance/check?x=1', undefined, 'https://x'), 600000)
  assert.equal(resolveTimeout('https://aiops.example/api/v1/config-backups/manual/9', undefined, 'https://x'), 600000)
  assert.equal(resolveTimeout({ url: 'https://aiops.example/api/v1/devices' }, undefined, 'https://x'), 15000)
})

test('显式 init.timeout 优先级最高', () => {
  assert.equal(resolveTimeout('/api/v1/devices', { timeout: 60000 }, 'https://x'), 60000)
  assert.equal(resolveTimeout('/api/v1/config-backups/manual/1', { timeout: 3000 }, 'https://x'), 3000)
  // 非法值回落到默认/白名单
  assert.equal(resolveTimeout('/api/v1/devices', { timeout: 0 }, 'https://x'), 15000)
  assert.equal(resolveTimeout('/api/v1/devices', { timeout: -5 }, 'https://x'), 15000)
})

test('前缀不同但相近的路径不会被误放宽', () => {
  // /manual-all-xxx 不是 manual-all，也不匹配 /manual/ 前缀
  assert.equal(resolveTimeout('/api/v1/config-backups/manual-all-extra', undefined, 'https://x'), 15000)
  assert.equal(resolveTimeout('/api/v1/config-backups/list', undefined, 'https://x'), 15000)
})

// ═══ 2. 行为：回归核心 bug ═══
test('【回归】配置备份耗时 20s 不再被 15s 掐断', async () => {
  const win = makeWin({ handler: slowFetch(20000) })
  installFetchGuard(win)
  const p = win.fetch('/api/v1/config-backups/manual/3', { method: 'POST' })
  win.__clock.advance(20000)
  const res = await p
  assert.equal(res.status, 200, '应当正常拿到响应而不是抛 AbortError')
})

test('【回归】真实对照：普通查询超 15s 仍会被掐断（守卫未被削弱）', async () => {
  const win = makeWin({ handler: slowFetch(20000) })
  installFetchGuard(win)
  const p = win.fetch('/api/v1/devices')
  win.__clock.advance(20000)
  await assert.rejects(p, (e) => {
    assert.equal(e.name, 'TimeoutError')
    assert.match(e.message, /请求超时（超过 15 秒）/)
    assert.match(e.message, /可能仍在后台执行/)
    return true
  })
})

test('限时内正常返回不被 abort', async () => {
  const win = makeWin({ handler: slowFetch(300) })
  installFetchGuard(win)
  const p = win.fetch('/api/v1/devices')
  win.__clock.advance(300)
  const res = await p
  assert.equal(res.status, 200)
})

test('显式 timeout 覆盖对长耗时路径同样生效', async () => {
  const win = makeWin({ handler: slowFetch(60000) })
  installFetchGuard(win)
  const p = win.fetch('/api/v1/config-backups/manual/1', { timeout: 3000 })
  win.__clock.advance(3000)
  await assert.rejects(p, (e) => e.name === 'TimeoutError')
})

// ═══ 3. 包装器的其它契约 ═══
test('非标准 timeout 字段不会透传给原生 fetch', async () => {
  let seen = null
  const win = makeWin({
    handler: () => async (input, init) => {
      seen = init
      return { status: 200 }
    },
  })
  installFetchGuard(win)
  await win.fetch('/api/v1/devices', { timeout: 5000, method: 'POST' })
  assert.ok(seen, '原生 fetch 应被调用')
  assert.equal(seen.timeout, undefined, 'timeout 必须被剥离')
  assert.equal(seen.method, 'POST', '其它字段应保留')
  assert.ok(seen.signal, '应注入 signal')
})

test('401 触发跳登录，且只跳一次', async () => {
  const win = makeWin({ handler: () => async () => ({ status: 401 }), pathname: '/devices' })
  installFetchGuard(win)
  await win.fetch('/api/v1/devices')
  assert.equal(win.location.href, '/login')
  assert.equal(win.__loginRedirecting, true)
  // 二次 401 不再重复改写
  win.location.href = ''
  await win.fetch('/api/v1/devices')
  assert.equal(win.location.href, '', '已在跳转中不应重复跳')
})

test('登录页上的 401 不触发跳转', async () => {
  const win = makeWin({ handler: () => async () => ({ status: 401 }), pathname: '/login' })
  installFetchGuard(win)
  await win.fetch('/api/v1/devices')
  assert.equal(win.location.href, '', '登录页不应再跳登录页')
})

test('调用方自带 signal 被尊重（可被外部 abort）', async () => {
  const win = makeWin({ handler: slowFetch(20000) })
  installFetchGuard(win)
  const ext = new AbortController()
  const p = win.fetch('/api/v1/config-backups/manual/1', { signal: ext.signal })
  ext.abort()
  await assert.rejects(p, (e) => e.name === 'AbortError')
})

test('已 aborted 的 signal 立即中止', async () => {
  const win = makeWin({ handler: slowFetch(20000) })
  installFetchGuard(win)
  const ext = new AbortController()
  ext.abort()
  const p = win.fetch('/api/v1/config-backups/manual/1', { signal: ext.signal })
  await assert.rejects(p, (e) => e.name === 'AbortError')
})

test('定时器在请求结束后被清理（无泄漏）', async () => {
  const win = makeWin({ handler: slowFetch(100) })
  installFetchGuard(win)
  const p = win.fetch('/api/v1/devices')
  win.__clock.advance(100)
  await p
  assert.equal(win.__clock.pending, 0, '不应残留定时器')
})

test('白名单表结构自洽（正则 + 正数超时 + 说明）', () => {
  assert.ok(LONG_OP_TIMEOUTS.length > 0)
  for (const [re, ms, desc] of LONG_OP_TIMEOUTS) {
    assert.ok(re instanceof RegExp, '第一项应为正则')
    assert.ok(typeof ms === 'number' && ms > DEFAULT_TIMEOUT, '长耗时超时必须大于默认值')
    assert.ok(typeof desc === 'string' && desc.length > 0, '应有说明')
  }
})

// ── 运行 ──
let pass = 0
let fail = 0
for (const { name, fn } of tests) {
  try {
    await fn()
    console.log(`  ✓ ${name}`)
    pass++
  } catch (e) {
    console.log(`  ✗ ${name}`)
    console.log(`      ${e && e.message}`)
    fail++
  }
}
console.log(`\n${pass} passed, ${fail} failed  (共 ${tests.length} 项)`)
process.exit(fail === 0 ? 0 : 1)
