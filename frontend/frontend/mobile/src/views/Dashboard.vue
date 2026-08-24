<template>
  <div class="p-4">
    <header class="mb-4 flex items-center justify-between">
      <div>
        <h1 class="text-lg font-bold text-ink-strong">监控概览</h1>
        <p class="text-xs text-ink-faint">{{ serverText }}</p>
      </div>
      <button @click="load" class="rounded-lg border border-line px-3 py-1.5 text-xs text-ink-muted active:bg-surface-2">
        刷新
      </button>
    </header>

    <!-- 统计卡片 -->
    <div class="mb-4 grid grid-cols-2 gap-3">
      <div class="rounded-2xl border border-line bg-surface p-4">
        <p class="text-xs text-ink-faint">设备总数</p>
        <p class="mt-1 text-2xl font-bold text-cyan-400">{{ stats.total_devices || 0 }}</p>
      </div>
      <div class="rounded-2xl border border-line bg-surface p-4">
        <p class="text-xs text-ink-faint">在线设备</p>
        <p class="mt-1 text-2xl font-bold text-emerald-400">{{ stats.online || 0 }}</p>
      </div>
      <div class="rounded-2xl border border-line bg-surface p-4">
        <p class="text-xs text-ink-faint">活跃告警</p>
        <p class="mt-1 text-2xl font-bold text-amber-400">{{ stats.active_alerts || 0 }}</p>
      </div>
      <div class="rounded-2xl border border-line bg-surface p-4">
        <p class="text-xs text-ink-faint">离线设备</p>
        <p class="mt-1 text-2xl font-bold text-red-400">{{ stats.offline || 0 }}</p>
      </div>
    </div>

    <!-- 在线率 -->
    <div class="mb-4 rounded-2xl border border-line bg-surface p-4">
      <div class="mb-2 flex items-center justify-between">
        <span class="text-xs text-ink-faint">设备在线率</span>
        <span class="text-sm font-semibold text-cyan-400">{{ onlineRate }}%</span>
      </div>
      <div class="h-2 overflow-hidden rounded-full bg-surface-2">
        <div class="h-full rounded-full bg-gradient-to-r from-cyan-500 to-emerald-400" :style="{ width: onlineRate + '%' }"></div>
      </div>
    </div>

    <!-- 最近告警 -->
    <div class="rounded-2xl border border-line bg-surface">
      <div class="flex items-center justify-between border-b border-line px-4 py-3">
        <span class="text-sm font-semibold text-ink">最近告警</span>
        <router-link to="/alerts" class="text-xs text-cyan-400">全部 ›</router-link>
      </div>
      <div v-if="recentAlerts.length === 0" class="px-4 py-6 text-center text-xs text-ink-faint">暂无告警</div>
      <div v-for="a in recentAlerts" :key="a.id" class="flex items-center gap-3 border-b border-line/60 px-4 py-3 last:border-0">
        <span class="h-2 w-2 shrink-0 rounded-full" :class="severityDot(a.severity)"></span>
        <div class="min-w-0 flex-1">
          <p class="truncate text-sm text-ink">{{ a.message || a.alert_name }}</p>
          <p class="text-xs text-ink-faint">{{ a.device_name || '' }} · {{ fmtTime(a.triggered_at || a.created_at) }}</p>
        </div>
        <span class="shrink-0 rounded px-1.5 py-0.5 text-[10px]" :class="severityBadge(a.severity)">{{ severityLabel(a.severity) }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { getDashboardOverview, getRecentAlerts } from '../api.js'
import { getServer } from '../store.js'

const stats = ref({})
const recentAlerts = ref([])
const loading = ref(false)
const serverText = computed(() => {
  const s = getServer()
  return s ? `${s.ip}${s.port ? ':' + s.port : ''}` : ''
})
const onlineRate = computed(() => {
  const t = stats.value.total_devices || 0
  if (!t) return 0
  return Math.round(((stats.value.online || 0) / t) * 100)
})

const severityDot = (s) => ({
  critical: 'bg-red-500', major: 'bg-amber-500', minor: 'bg-yellow-500', warning: 'bg-cyan-400',
}[s] || 'bg-ink-faint')
const severityBadge = (s) => ({
  critical: 'bg-red-500/15 text-red-400', major: 'bg-amber-500/15 text-amber-400',
  minor: 'bg-yellow-500/15 text-yellow-400', warning: 'bg-cyan-500/15 text-cyan-400',
}[s] || 'bg-ink-faint/15 text-ink-muted')
const severityLabel = (s) => ({ critical: '严重', major: '重要', minor: '次要', warning: '警告' }[s] || s || '')
const fmtTime = (t) => {
  if (!t) return ''
  const d = new Date(t)
  if (isNaN(d)) return String(t).slice(5, 16).replace('T', ' ')
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

async function load() {
  if (loading.value) return
  loading.value = true
  try {
    const [overview, alerts] = await Promise.all([
      getDashboardOverview().catch(() => ({})),
      getRecentAlerts().catch(() => []),
    ])
    // 后端统一返回 {code, message, data}，取 data 字段（兼容直接返回对象的情况）
    stats.value = (overview && overview.data) ? overview.data : (overview || {})
    const alertsData = Array.isArray(alerts) ? alerts : (alerts?.data || [])
    recentAlerts.value = Array.isArray(alertsData) ? alertsData.slice(0, 6) : []
  } catch (e) {
    console.error(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
