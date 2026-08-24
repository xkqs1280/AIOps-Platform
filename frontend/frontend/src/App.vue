<template>
  <div class="flex h-screen bg-slate-950 text-slate-200 overflow-hidden">
    <!-- 侧边导航（登录页不显示） -->
    <aside
      v-if="route.path !== '/login'"
      class="w-60 bg-slate-900/70 border-r border-slate-800 flex flex-col shrink-0 backdrop-blur"
      style="background-image: var(--gradient-sidebar); background-color: rgba(15, 23, 42, 0.72)"
    >
      <div class="px-5 py-4 border-b border-slate-800 flex items-center gap-3">
        <img src="/logo.svg" class="w-9 h-9 shrink-0 drop-shadow-[0_0_8px_rgba(34,211,238,0.35)]" alt="AIOps" />
        <div class="min-w-0">
          <h1 class="text-[15px] font-bold grad-text tracking-wide truncate">AIOps 智能运维</h1>
          <p class="text-[11px] text-slate-500 mt-0.5">托管平台 v{{ versionText }}</p>
        </div>
      </div>

      <nav class="flex-1 py-3 px-3 overflow-y-auto space-y-0.5">
        <div class="text-[11px] font-semibold text-slate-500 tracking-wider px-3 pb-2 pt-1">运维管理</div>
        <router-link
          v-for="item in mainNav"
          :key="item.path"
          :to="item.path"
          class="nav-item relative"
          :class="isActive(item.path) ? 'active' : ''"
        >
          <span
            v-if="isActive(item.path)"
            class="absolute left-0 top-1/2 -translate-y-1/2 w-0.5 h-5 rounded-full grad-brand"
          ></span>
          <component :is="item.icon" class="nav-icon" />
          <span class="truncate">{{ item.label }}</span>
        </router-link>

        <!-- 系统设置分组 -->
        <div class="pt-4">
          <div class="text-[11px] font-semibold text-slate-500 tracking-wider px-3 pb-2">系统</div>
          <button
            @click="settingsOpen = !settingsOpen"
            class="w-full nav-item relative"
            :class="isInSettings ? 'active' : ''"
          >
            <component :is="Cog6ToothIcon" class="nav-icon" />
            <span class="flex-1 text-left truncate">系统设置</span>
            <ChevronRightIcon
              class="w-4 h-4 shrink-0 transition-transform duration-150"
              :class="settingsOpen ? 'rotate-90' : ''"
            />
          </button>
          <div v-show="settingsOpen" class="ml-3 mt-0.5 pl-3 border-l border-slate-800 space-y-0.5">
            <router-link
              v-for="item in settingsNav"
              :key="item.path"
              :to="item.path"
              class="nav-item"
              :class="isActive(item.path) ? 'active' : ''"
            >
              <component :is="item.icon" class="nav-icon" />
              <span class="truncate">{{ item.label }}</span>
            </router-link>
          </div>
        </div>
      </nav>

      <div class="px-5 py-3 border-t border-slate-800 text-[11px] text-slate-600">
        2026 AIOps Platform · v{{ versionText }}
      </div>
    </aside>

    <!-- 主内容区 -->
    <main
      class="flex-1 overflow-auto"
      style="background-image: radial-gradient(100% 60% at 50% 0%, rgba(14, 165, 233, 0.05) 0%, rgba(2, 6, 23, 0) 60%)"
    >
      <router-view />
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getSystemVersion } from './api/index.js'
import {
  ChartBarIcon,
  ServerStackIcon,
  BellAlertIcon,
  ShareIcon,
  ArchiveBoxIcon,
  MagnifyingGlassIcon,
  EyeIcon,
  ArrowsRightLeftIcon,
  ShieldCheckIcon,
  ClipboardDocumentCheckIcon,
  Cog6ToothIcon,
  EnvelopeIcon,
  BellIcon,
  UserIcon,
  KeyIcon,
  DocumentTextIcon,
  ArrowUpTrayIcon,
  ChevronRightIcon,
} from '@heroicons/vue/24/outline'
import { playAlert, playRecovered, unlock } from './utils/voiceAlert'

const route = useRoute()
const versionText = ref('4.0')

const mainNav = [
  { path: '/', label: '监控大屏', icon: ChartBarIcon },
  { path: '/devices', label: '设备管理', icon: ServerStackIcon },
  { path: '/alerts', label: '告警管理', icon: BellAlertIcon },
  { path: '/topology', label: '拓扑发现', icon: ShareIcon },
  { path: '/config-backup', label: '配置备份', icon: ArchiveBoxIcon },
  { path: '/inspection', label: 'H3C 巡检', icon: MagnifyingGlassIcon },
  { path: '/business-monitor', label: '重要业务监控', icon: EyeIcon },
  { path: '/lifecycle', label: '生命周期', icon: ArrowsRightLeftIcon },
  { path: '/security', label: '安全监控', icon: ShieldCheckIcon },
  { path: '/compliance', label: '等保合规', icon: ClipboardDocumentCheckIcon },
]

const settingsNav = [
  { path: '/settings/mail', label: '邮件告警', icon: EnvelopeIcon },
  { path: '/settings/alert-rules', label: '告警规则', icon: BellIcon },
  { path: '/settings/account', label: '账号管理', icon: UserIcon },
  { path: '/settings/license', label: '授权管理', icon: KeyIcon },
  { path: '/settings/audit-logs', label: '审计日志', icon: DocumentTextIcon },
  { path: '/settings/upgrade', label: '系统升级', icon: ArrowUpTrayIcon },
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
  getSystemVersion().then((v) => { versionText.value = v.version || '4.0' }).catch(() => {})
})
onUnmounted(() => {
  if (es) es.close()
})
</script>
