<template>
  <div class="p-4">
    <header class="mb-4 flex items-center justify-between">
      <h1 class="text-lg font-bold text-ink-strong">更多功能</h1>
    </header>

    <!-- 当前服务器 -->
    <div class="mb-4 rounded-2xl border border-line bg-surface p-4">
      <p class="mb-1 text-xs text-ink-faint">当前服务器</p>
      <p class="text-sm font-medium text-cyan-400">{{ serverText }}</p>
      <button @click="goSwitchServer" class="mt-2 rounded-lg border border-line px-3 py-1.5 text-xs text-ink-muted">切换服务器</button>
    </div>

    <!-- 功能入口 -->
    <div class="mb-4 grid grid-cols-2 gap-2.5">
      <router-link
        v-for="item in menu"
        :key="item.path"
        :to="item.path"
        class="flex items-center gap-3 rounded-2xl border border-line bg-surface p-3.5 active:bg-surface-2"
      >
        <span class="text-xl">{{ item.icon }}</span>
        <div>
          <p class="text-sm font-medium text-ink-strong">{{ item.label }}</p>
          <p class="text-[10px] text-ink-faint">{{ item.desc }}</p>
        </div>
      </router-link>
    </div>

    <!-- 关于 -->
    <div class="rounded-2xl border border-line bg-surface p-4 text-center">
      <p class="text-sm font-semibold text-ink">AIOps 智能运维托管平台</p>
      <p class="mt-1 text-xs text-ink-faint">v{{ versionText }} · 移动端</p>
    </div>

    <!-- 退出 -->
    <button
      @click="doLogout"
      class="mt-4 w-full rounded-2xl border border-red-800/50 bg-red-950/30 py-3 text-sm font-medium text-red-400 active:bg-red-950/50"
    >
      退出登录
    </button>
  </div>
</template>

<script setup>
import { computed, ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { getServer, setToken } from '../store.js'
import { stopAlertPolling } from '../notifications.js'
import { getSystemVersion } from '../api.js'

const router = useRouter()
const versionText = ref('4.0')

onMounted(() => {
  getSystemVersion().then((v) => { versionText.value = v.version || '4.0' }).catch(() => {})
})

const serverText = computed(() => {
  const s = getServer()
  return s ? `${s.ip}${s.port ? ':' + s.port : ''}` : '未配置'
})

const menu = [
  { path: '/lifecycle', label: '生命周期', icon: '🔄', desc: '续保/维保提醒' },
  { path: '/compliance', label: '等保合规', icon: '🛡', desc: '合规检查与评分' },
  { path: '/security', label: '安全监控', icon: '⚠️', desc: '外部威胁态势' },
  { path: '/inspection', label: '设备巡检', icon: '🔍', desc: 'H3C 巡检任务' },
  { path: '/account', label: '账号设置', icon: '👤', desc: '修改密码/账号' },
  { path: '/system-upgrade', label: '系统升级', icon: '📦', desc: '一键升级平台' },
]

function goSwitchServer() {
  router.push('/login')
}

function doLogout() {
  stopAlertPolling()
  setToken('')
  router.push('/login')
}
</script>
