<template>
  <div class="flex h-screen bg-gray-950 text-gray-100 overflow-hidden">
    <!-- 侧边导航（登录页不显示） -->
    <aside v-if="route.path !== '/login'" class="w-56 bg-gray-900 border-r border-gray-800 flex flex-col shrink-0">
      <div class="px-5 py-4 border-b border-gray-800 flex items-center gap-3">
        <img src="/logo.svg" class="w-9 h-9 shrink-0" alt="AIOps" />
        <div class="min-w-0">
          <h1 class="text-base font-bold text-cyan-400 tracking-wide truncate">AIOps 智能运维</h1>
          <p class="text-xs text-gray-500 mt-0.5">托管平台 v3.8</p>
        </div>
      </div>
      <nav class="flex-1 py-3 space-y-0.5 px-2 overflow-y-auto">
        <router-link
          v-for="item in mainNav"
          :key="item.path"
          :to="item.path"
          class="flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-colors"
          :class="isActive(item.path) ? 'bg-cyan-500/15 text-cyan-400' : 'text-gray-400 hover:bg-gray-800 hover:text-gray-200'"
        >
          <span class="text-base w-5 text-center">{{ item.icon }}</span>
          {{ item.label }}
        </router-link>

        <!-- 系统设置分组 -->
        <div>
          <button
            @click="settingsOpen = !settingsOpen"
            class="w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-colors"
            :class="isInSettings ? 'bg-cyan-500/15 text-cyan-400' : 'text-gray-400 hover:bg-gray-800 hover:text-gray-200'"
          >
            <span class="text-base w-5 text-center">⚙️</span>
            <span class="flex-1 text-left">系统设置</span>
            <svg
              class="w-3.5 h-3.5 transition-transform duration-200"
              :class="settingsOpen ? 'rotate-90' : ''"
              viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"
            ><path d="M9 6l6 6-6 6" /></svg>
          </button>
          <div v-show="settingsOpen" class="ml-3 mt-0.5 pl-3 border-l border-gray-800 space-y-0.5">
            <router-link
              v-for="item in settingsNav"
              :key="item.path"
              :to="item.path"
              class="flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors"
              :class="isActive(item.path) ? 'bg-cyan-500/15 text-cyan-400' : 'text-gray-400 hover:bg-gray-800 hover:text-gray-200'"
            >
              <span class="text-base w-5 text-center">{{ item.icon }}</span>
              {{ item.label }}
            </router-link>
          </div>
        </div>
      </nav>
      <div class="px-4 py-3 border-t border-gray-800 text-xs text-gray-600">
        2026 AIOps Platform
      </div>
    </aside>

    <!-- 主内容区 -->
    <main class="flex-1 overflow-auto">
      <router-view />
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { playAlert, playRecovered, unlock } from './utils/voiceAlert'

const route = useRoute()

const mainNav = [
  { path: '/', label: '监控大屏', icon: '📊' },
  { path: '/devices', label: '设备管理', icon: '🖥️' },
  { path: '/alerts', label: '告警管理', icon: '🔔' },
  { path: '/topology', label: '拓扑发现', icon: '🔗' },
  { path: '/config-backup', label: '配置备份', icon: '💾' },
  { path: '/inspection', label: 'H3C 巡检', icon: '🔍' },
  { path: '/business-monitor', label: '重要业务监控', icon: '📷' },
  { path: '/lifecycle', label: '生命周期', icon: '🔄' },
  { path: '/security', label: '安全监控', icon: '🛡️' },
  { path: '/compliance', label: '等保合规', icon: '✅' },
]

const settingsNav = [
  { path: '/settings/mail', label: '邮件告警', icon: '📧' },
  { path: '/settings/alert-rules', label: '告警规则', icon: '⚙️' },
  { path: '/settings/account', label: '账号管理', icon: '👤' },
  { path: '/settings/license', label: '授权管理', icon: '🔑' },
  { path: '/settings/audit-logs', label: '审计日志', icon: '📋' },
]

// 系统设置分组折叠状态：位于系统设置子页时自动展开
const settingsOpen = ref(true)
const isInSettings = computed(() => route.path.startsWith('/settings'))
watch(isInSettings, (v) => { if (v) settingsOpen.value = true })

function isActive(path) {
  if (path === '/') return route.path === '/'
  return route.path.startsWith(path)
}

// ---------- 语音告警：激活 + SSE 实时推送 ----------
function activateVoice() {
  unlock()
}
function onFirstGesture() {
  activateVoice()
  window.removeEventListener('click', onFirstGesture)
  window.removeEventListener('keydown', onFirstGesture)
}
window.addEventListener('click', onFirstGesture)
window.addEventListener('keydown', onFirstGesture)

let es = null
let sseInited = false

function connectSse() {
  if (es) es.close()
  es = new EventSource('/api/v1/alerts/stream')
  es.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data)
      if (!d || !d.type) return
      if (d.type === 'init') {
        sseInited = true
        return
      }
      if (!sseInited) return
      if (d.type === 'alert') playAlert(d.item)
      else if (d.type === 'recovered') playRecovered(d.item)
    } catch { /* 忽略解析错误 */ }
  }
  es.onerror = async () => {
    // 网络中断由 EventSource 自动重连；会话失效则跳登录
    try {
      const res = await fetch('/api/v1/auth/me')
      if (res.status === 401 && window.location.pathname !== '/login') {
        window.location.href = '/login'
      }
    } catch { /* 网络抖动，等待自动重连 */ }
  }
}

onMounted(() => {
  connectSse()
})
onUnmounted(() => {
  if (es) es.close()
})
</script>
