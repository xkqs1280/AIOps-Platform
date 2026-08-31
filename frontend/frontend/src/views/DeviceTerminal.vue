<template>
  <div class="flex flex-col h-full bg-app text-ink-strong">
    <!-- Header -->
    <div class="flex items-center justify-between border-b border-line bg-surface/50 px-4 py-3">
      <div class="flex items-center gap-3">
        <button
          @click="$router.push('/devices')"
          class="flex items-center gap-1 text-ink-muted hover:text-ink transition-colors"
        >
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" />
          </svg>
          <span>返回</span>
        </button>
        <h1 class="text-lg font-bold">{{ device?.name || '设备' }}</h1>
        <span class="font-mono text-sm text-ink-muted">{{ device?.ip }}</span>
        <span class="px-2 py-0.5 rounded-full text-xs font-medium" :class="statusColor(device?.status)">
          {{ statusLabel(device?.status) }}
        </span>
      </div>
      <div class="flex items-center gap-2">
        <span
          v-if="connState === 'connected'"
          class="flex items-center gap-1.5 text-xs text-emerald-500"
        >
          <span class="h-2 w-2 rounded-full bg-emerald-500"></span>
          已连接
        </span>
        <span
          v-else-if="connState === 'connecting'"
          class="flex items-center gap-1.5 text-xs text-amber-500"
        >
          <span class="h-2 w-2 rounded-full bg-amber-500 animate-pulse"></span>
          连接中…
        </span>
        <span
          v-else
          class="flex items-center gap-1.5 text-xs text-red-400"
        >
          <span class="h-2 w-2 rounded-full bg-red-400"></span>
          已断开
        </span>
        <button
          @click="reconnect"
          class="rounded-lg border border-line bg-surface-2 px-3 py-1.5 text-xs text-ink-muted transition-colors hover:bg-hover"
        >
          ↻ 重连
        </button>
      </div>
    </div>

    <!-- Terminal -->
    <div ref="termWrapRef" class="relative flex-1 overflow-hidden bg-[#0f172a]">
      <div ref="termRef" class="h-full w-full" />
      <div
        v-if="connState === 'closed' && closeMsg"
        class="absolute bottom-0 left-0 right-0 bg-red-500/10 border-t border-red-500/30 px-4 py-2 text-xs text-red-400"
      >
        {{ closeMsg }}
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import { Terminal } from 'xterm'
import { FitAddon } from '@xterm/addon-fit'
import 'xterm/css/xterm.css'
import { getDevice } from '../api/index.js'

const route = useRoute()
const deviceId = route.params.id

const termWrapRef = ref(null)
const termRef = ref(null)
const device = ref(null)
const connState = ref('connecting') // connecting / connected / closed
const closeMsg = ref('')

let terminal = null
let fitAddon = null
let ws = null

function statusLabel(s) {
  return { online: '在线', warning: '告警', offline: '离线', unknown: '未知' }[s] || '未知'
}
function statusColor(s) {
  const map = {
    online: 'bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-400',
    warning: 'bg-amber-100 text-amber-700 dark:bg-amber-900/50 dark:text-amber-400',
    offline: 'bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-400',
    unknown: 'bg-hover/50 text-ink-muted border border-line-strong',
  }
  return map[s] || map.unknown
}

function wsUrl() {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/api/v1/devices/${deviceId}/terminal`
}

function connect() {
  if (ws) {
    try { ws.close() } catch (e) { /* noop */ }
    ws = null
  }
  connState.value = 'connecting'
  closeMsg.value = ''
  ws = new WebSocket(wsUrl())
  ws.onopen = () => {
    connState.value = 'connected'
    terminal?.writeln('\x1b[1;32m[已连接设备，可输入命令]\x1b[0m\r\n')
  }
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data)
      if (msg.type === 'output') {
        terminal?.write(msg.data)
      } else if (msg.type === 'error') {
        terminal?.writeln(`\r\n\x1b[1;31m[${msg.message}]\x1b[0m`)
      } else if (msg.type === 'closed') {
        connState.value = 'closed'
        closeMsg.value = msg.message || '连接已断开'
        terminal?.writeln(`\r\n\x1b[1;31m[${msg.message || '连接已断开'}]\x1b[0m`)
      }
    } catch (e) { /* ignore */ }
  }
  ws.onerror = () => {
    connState.value = 'closed'
    closeMsg.value = 'WebSocket 连接错误'
  }
  ws.onclose = () => {
    if (connState.value !== 'closed') {
      connState.value = 'closed'
      closeMsg.value = '连接已断开'
    }
  }
}

function reconnect() {
  if (terminal) {
    terminal.reset()
  }
  connect()
}

function onResize() {
  if (fitAddon) fitAddon.fit()
  if (ws && ws.readyState === WebSocket.OPEN && terminal) {
    ws.send(JSON.stringify({ type: 'resize', cols: terminal.cols, rows: terminal.rows }))
  }
}

onMounted(async () => {
  try {
    device.value = await getDevice(deviceId)
  } catch (e) { /* device info optional */ }

  await nextTick()
  terminal = new Terminal({
    fontFamily: 'Consolas, "Courier New", monospace',
    fontSize: 14,
    cursorBlink: true,
    scrollback: 5000,
    rightClickSelectsWord: true, // 右键选择单词
    theme: {
      background: '#0f172a',
      foreground: '#e2e8f0',
      cursor: '#67e8f9',
      selectionBackground: 'rgba(34,211,238,0.3)',
    },
  })
  fitAddon = new FitAddon()
  terminal.loadAddon(fitAddon)
  terminal.open(termRef.value)
  fitAddon.fit()

  // 复制 / 粘贴支持（xterm 默认不绑定快捷键）：
  // - Ctrl+Shift+C：复制选中文本
  // - Ctrl+C：有选中时复制；无选中时仍发送中断信号给设备
  // - Ctrl+Shift+V：粘贴剪贴板内容
  terminal.attachCustomKeyEventHandler((event) => {
    const mod = event.ctrlKey || event.metaKey
    if (mod && event.key.toLowerCase() === 'c') {
      if (terminal.hasSelection()) {
        navigator.clipboard.writeText(terminal.getSelection()).catch(() => {})
        event.preventDefault()
        return false // 拦截，不发送给设备
      }
      return true // 无选中：Ctrl+C 仍作为中断信号发送
    }
    if (mod && event.shiftKey && event.key.toLowerCase() === 'v') {
      navigator.clipboard.readText().then((text) => {
        if (text) terminal.paste(text)
      }).catch(() => {})
      event.preventDefault()
      return false
    }
    return true
  })
  // 点击终端时清除选区，避免误触发复制
  terminal.textarea.addEventListener('mouseup', () => {
    setTimeout(() => { if (!terminal.hasSelection()) terminal.clearSelection() }, 10)
  })

  terminal.onData((data) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'input', data }))
    }
  })
  terminal.onResize(({ cols, rows }) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'resize', cols, rows }))
    }
  })

  window.addEventListener('resize', onResize)
  const observer = new ResizeObserver(() => onResize())
  observer.observe(termWrapRef.value)
  termWrapRef.value.__resizeObserver = observer

  connect()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  if (termWrapRef.value?.__resizeObserver) {
    termWrapRef.value.__resizeObserver.disconnect()
  }
  if (ws) {
    try { ws.close() } catch (e) { /* noop */ }
    ws = null
  }
  if (terminal) {
    terminal.dispose()
    terminal = null
  }
})
</script>
