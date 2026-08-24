<template>
  <div class="p-4">
    <header class="mb-3 flex items-center justify-between">
      <h1 class="text-lg font-bold text-slate-100">告警管理</h1>
      <div class="flex gap-2">
        <button @click="load(true)" class="rounded-lg border border-slate-700 px-3 py-1.5 text-xs text-slate-300">刷新</button>
        <button v-if="alerts.length" @click="doClear" class="rounded-lg border border-red-700/50 px-3 py-1.5 text-xs text-red-400">清除全部</button>
      </div>
    </header>

    <!-- 状态筛选 -->
    <div class="mb-3 flex gap-2">
      <button
        v-for="f in filters"
        :key="f.value"
        @click="statusFilter = f.value; load(true)"
        class="rounded-full px-3 py-1 text-xs"
        :class="statusFilter === f.value ? 'bg-cyan-600 text-white' : 'border border-slate-700 text-slate-400'"
      >
        {{ f.label }}
      </button>
    </div>

    <div v-if="alerts.length" class="space-y-2.5">
      <div v-for="a in alerts" :key="a.id" class="rounded-2xl border border-slate-800 bg-slate-900 p-3.5">
        <div class="flex items-start gap-2.5">
          <span class="mt-1 h-2 w-2 shrink-0 rounded-full" :class="sevDot(a.severity)"></span>
          <div class="min-w-0 flex-1">
            <p class="text-sm font-medium text-slate-100">{{ a.message || a.alert_name }}</p>
            <p class="mt-0.5 text-xs text-slate-500">{{ a.device_name || '' }}{{ a.device_ip ? ' (' + a.device_ip + ')' : '' }}</p>
            <p class="mt-0.5 text-xs text-slate-600">{{ fmtTime(a.triggered_at || a.created_at) }}</p>
          </div>
          <span class="shrink-0 rounded px-1.5 py-0.5 text-[10px]" :class="sevBadge(a.severity)">{{ sevLabel(a.severity) }}</span>
        </div>
      </div>
      <p v-if="hasMore" class="py-2 text-center text-xs text-slate-600">上滑加载更多</p>
    </div>
    <div v-else class="py-16 text-center text-sm text-slate-600">{{ loading ? '加载中...' : '暂无告警' }}</div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { getAlerts, clearAlerts } from '../api.js'

const alerts = ref([])
const statusFilter = ref('')
const page = ref(1)
const hasMore = ref(false)
const loading = ref(false)

const filters = [
  { label: '全部', value: '' },
  { label: '活动', value: 'active' },
  { label: '已处理', value: 'resolved' },
]

const sevDot = (s) => ({ critical: 'bg-red-500', major: 'bg-amber-500', minor: 'bg-yellow-500', warning: 'bg-cyan-400' }[s] || 'bg-slate-500')
const sevBadge = (s) => ({ critical: 'bg-red-500/15 text-red-400', major: 'bg-amber-500/15 text-amber-400', minor: 'bg-yellow-500/15 text-yellow-400', warning: 'bg-cyan-500/15 text-cyan-400' }[s] || 'bg-slate-500/15 text-slate-400')
const sevLabel = (s) => ({ critical: '严重', major: '重要', minor: '次要', warning: '警告' }[s] || s || '')
const fmtTime = (t) => {
  if (!t) return ''
  const d = new Date(t)
  if (isNaN(d)) return String(t).slice(0, 16).replace('T', ' ')
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

async function load(reset = false) {
  if (loading.value) return
  if (reset) { page.value = 1; alerts.value = [] }
  loading.value = true
  try {
    const res = await getAlerts({ page: page.value, page_size: 20, status: statusFilter.value || undefined })
    const items = res?.items || res?.data || []
    alerts.value = reset ? items : [...alerts.value, ...items]
    const total = res?.total ?? items.length
    hasMore.value = page.value * 20 < total
    if (hasMore.value) page.value += 1
  } catch (e) { console.error(e) }
  finally { loading.value = false }
}

async function doClear() {
  if (!confirm('确认清除全部告警？')) return
  await clearAlerts()
  load(true)
}

function onScroll() {
  const el = document.scrollingElement
  if (el && el.scrollTop + el.clientHeight >= el.scrollHeight - 100) load()
}

onMounted(() => {
  load(true)
  window.addEventListener('scroll', onScroll, { passive: true })
})
onUnmounted(() => window.removeEventListener('scroll', onScroll))
</script>
