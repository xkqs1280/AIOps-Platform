<template>
  <div class="p-4 pb-10">
    <!-- 返回 + 标题 -->
    <div class="mb-3 flex items-center justify-between">
      <div class="flex items-center gap-1">
        <button @click="$router.back()" class="text-sm text-ink-muted active:text-ink">←</button>
        <h1 class="text-lg font-bold text-ink-strong">重要业务监控</h1>
      </div>
      <div class="flex items-center gap-2">
        <button @click="loadAll" class="text-xs text-cyan-400 active:text-cyan-300">刷新</button>
        <button
          @click="probeAll"
          :disabled="probingAll"
          class="rounded-xl bg-purple-600/90 px-3 py-1.5 text-xs font-medium text-white active:bg-purple-500 disabled:opacity-50"
        >{{ probingAll ? '探测中' : '探测全部' }}</button>
      </div>
    </div>

    <!-- 统计 -->
    <div class="mb-3 grid grid-cols-4 gap-2">
      <div v-for="s in stats" :key="s.label" class="rounded-2xl border border-line bg-surface px-2 py-2.5 text-center">
        <p class="text-base font-bold" :class="s.color || 'text-ink-strong'">{{ s.value }}</p>
        <p class="mt-0.5 text-[10px] text-ink-faint">{{ s.label }}</p>
      </div>
    </div>

    <!-- Tab -->
    <div class="mb-3 flex gap-2">
      <button
        v-for="t in tabs"
        :key="t.key"
        @click="switchTab(t.key)"
        class="rounded-full px-4 py-1.5 text-xs font-medium"
        :class="activeTab === t.key ? 'bg-cyan-600 text-white' : 'border border-line text-ink-muted'"
      >{{ t.label }}</button>
    </div>

    <!-- 终端列表 -->
    <template v-if="activeTab === 'terminals'">
      <!-- 分组筛选 -->
      <div class="mb-3 flex gap-2 overflow-x-auto pb-1" style="scrollbar-width: none">
        <button
          v-for="g in groupChips"
          :key="g.id ?? 'all'"
          @click="groupFilter = g.id ?? null; load(true)"
          class="shrink-0 rounded-full px-3 py-1.5 text-xs"
          :class="groupFilter === (g.id ?? null) ? 'bg-surface-2 text-cyan-400 border border-cyan-500/40' : 'border border-line text-ink-muted'"
        >{{ g.name }}</button>
      </div>

      <div v-if="terminals.length" class="space-y-2.5">
        <div v-for="t in terminals" :key="t.id" class="rounded-2xl border border-line bg-surface p-3.5">
          <div class="flex items-center gap-2.5">
            <span class="h-2.5 w-2.5 shrink-0 rounded-full" :class="statusDot(t.status)"></span>
            <div class="min-w-0 flex-1">
              <p class="truncate text-sm font-medium text-ink-strong">{{ t.name }}</p>
              <p class="truncate text-xs text-ink-faint">{{ groupName(t.group_id) }} · {{ t.ip }}</p>
            </div>
            <span class="shrink-0 text-xs" :class="statusTextColor(t.status)">{{ statusText(t.status) }}</span>
          </div>
          <div class="mt-2 flex items-center justify-between border-t border-line/60 pt-2">
            <span class="text-[10px] text-ink-faint">最后在线 {{ fmtTime(t.last_online_at) }}</span>
            <button
              @click="probeOne(t)"
              :disabled="probingId === t.id"
              class="rounded-lg border border-purple-500/40 bg-purple-500/10 px-2.5 py-1 text-[11px] text-purple-400 active:bg-purple-500/20 disabled:opacity-50"
            >{{ probingId === t.id ? '探测中' : '立即探测' }}</button>
          </div>
        </div>
        <p v-if="hasMore" class="py-2 text-center text-xs text-ink-faint">上滑加载更多</p>
      </div>
      <div v-else class="py-16 text-center text-sm text-ink-faint">{{ loading ? '加载中...' : '暂无终端' }}</div>
    </template>

    <!-- 告警记录 -->
    <template v-else>
      <div v-if="bizAlerts.length" class="space-y-2.5">
        <div v-for="a in bizAlerts" :key="a.id" class="rounded-2xl border border-line bg-surface p-3">
          <div class="flex items-center justify-between gap-2">
            <p class="truncate text-sm text-ink">{{ a.terminal_name }}
              <span class="text-[10px] text-ink-faint font-mono">({{ a.terminal_ip }})</span>
            </p>
            <span class="shrink-0 rounded px-1.5 py-0.5 text-[10px]" :class="a.alert_type === 'offline' ? 'bg-red-500/15 text-red-400' : 'bg-green-500/15 text-green-400'">
              {{ a.alert_type === 'offline' ? '离线告警' : '已恢复' }}
            </span>
          </div>
          <p v-if="a.message" class="mt-1 text-xs text-ink-muted">{{ a.message }}</p>
          <div class="mt-1.5 flex items-center justify-between">
            <span class="text-[10px] text-ink-faint">{{ fmtTime(a.created_at) }}</span>
            <button @click="removeAlert(a)" class="text-[11px] text-red-400 active:text-red-300">删除</button>
          </div>
        </div>
        <p v-if="alertHasMore" class="py-2 text-center text-xs text-ink-faint">上滑加载更多</p>
      </div>
      <div v-else class="py-16 text-center text-sm text-ink-faint">{{ loading ? '加载中...' : '暂无告警记录' }}</div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import {
  getBizGroups, getBizSummary, getBizTerminals, getBizAlerts,
  probeBizTerminal, probeBizAll, deleteBizAlert,
} from '../api.js'

const activeTab = ref('terminals')
const tabs = [
  { key: 'terminals', label: '终端状态' },
  { key: 'alerts', label: '告警记录' },
]

const summary = ref({})
const groups = ref([])
const groupFilter = ref(null)
const terminals = ref([])
const bizAlerts = ref([])
const loading = ref(false)
const probingAll = ref(false)
const probingId = ref(null)

// 终端分页
const page = ref(1)
const hasMore = ref(false)
// 告警分页
const alertPage = ref(1)
const alertHasMore = ref(false)
const alertTotal = ref(0)

const stats = computed(() => [
  { label: '终端', value: summary.value.total ?? 0 },
  { label: '在线', value: summary.value.online ?? 0, color: 'text-emerald-400' },
  { label: '离线', value: summary.value.offline ?? 0, color: 'text-red-400' },
  { label: '离线告警', value: summary.value.offline_alerts ?? 0, color: 'text-amber-400' },
])

const groupChips = computed(() => {
  const all = [{ id: null, name: `全部 ${groups.value.reduce((s, g) => s + (g.terminal_count || 0), 0)}` }]
  return [...all, ...groups.value.map(g => ({ ...g, name: `${g.name} ${g.terminal_count ?? 0}` }))]
})

const groupName = (gid) => (groups.value.find(g => g.id === gid) || {}).name || '-'

const statusDot = (s) => ({ online: 'bg-emerald-400', offline: 'bg-red-400', unknown: 'bg-ink-faint' }[s] || 'bg-ink-faint')
const statusText = (s) => ({ online: '在线', offline: '离线', unknown: '未知' }[s] || s || '—')
const statusTextColor = (s) => ({ online: 'text-emerald-400', offline: 'text-red-400', unknown: 'text-ink-muted' }[s] || 'text-ink-muted')

function fmtTime(v) {
  if (!v) return '—'
  const d = new Date(v)
  if (isNaN(d.getTime())) return String(v).slice(5, 16)
  const p = (n) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

async function loadGroups() {
  try {
    const data = await getBizGroups()
    groups.value = (data && data.items) || []
  } catch (e) { console.error(e) }
}
async function loadSummary() {
  try {
    summary.value = await getBizSummary()
  } catch (e) { console.error(e) }
}
async function loadTerminals(reset = false) {
  if (loading.value) return
  if (reset) { page.value = 1; terminals.value = [] }
  loading.value = true
  try {
    const data = await getBizTerminals({
      page: page.value,
      page_size: 20,
      group_id: groupFilter.value || undefined,
    })
    const items = (data && data.items) || []
    const total = data && data.total ? data.total : items.length
    terminals.value = reset ? items : [...terminals.value, ...items]
    hasMore.value = page.value * 20 < total
    if (hasMore.value) page.value += 1
  } catch (e) {
    console.error(e)
  } finally {
    loading.value = false
  }
}
async function loadAlerts(reset = false) {
  if (loading.value) return
  if (reset) { alertPage.value = 1; bizAlerts.value = [] }
  loading.value = true
  try {
    const data = await getBizAlerts({ page: alertPage.value, page_size: 20 })
    const items = (data && data.items) || []
    alertTotal.value = data && data.total ? data.total : items.length
    bizAlerts.value = reset ? items : [...bizAlerts.value, ...items]
    alertHasMore.value = alertPage.value * 20 < alertTotal.value
    if (alertHasMore.value) alertPage.value += 1
  } catch (e) {
    console.error(e)
  } finally {
    loading.value = false
  }
}

function switchTab(key) {
  activeTab.value = key
  if (key === 'terminals') {
    if (!terminals.value.length) loadTerminals(true)
  } else if (key === 'alerts' && !bizAlerts.value.length) {
    loadAlerts(true)
  }
}

async function probeOne(t) {
  probingId.value = t.id
  try {
    await probeBizTerminal(t.id)
    await Promise.all([loadSummary(), loadTerminals(true)])
  } catch (e) {
    alert('探测失败：' + (e.message || '网络错误'))
  } finally {
    probingId.value = null
  }
}
async function probeAll() {
  probingAll.value = true
  try {
    const data = await probeBizAll()
    await Promise.all([loadSummary(), loadTerminals(true)])
    alert(`探测完成，共 ${(data && data.probed) || 0} 台终端`)
  } catch (e) {
    alert('探测失败：' + (e.message || '网络错误'))
  } finally {
    probingAll.value = false
  }
}
async function removeAlert(a) {
  if (!confirm(`删除告警记录？\n${a.terminal_name}（${a.terminal_ip}）`)) return
  try {
    await deleteBizAlert(a.id)
    loadAlerts(true)
  } catch (e) {
    alert('删除失败：' + (e.message || '网络错误'))
  }
}

async function loadAll() {
  await Promise.all([loadGroups(), loadSummary()])
  activeTab.value === 'terminals' ? loadTerminals(true) : loadAlerts(true)
}

function onScroll() {
  const el = document.scrollingElement
  if (!el) return
  if (activeTab.value === 'terminals') {
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 100 && !loading.value) loadTerminals()
  } else if (el.scrollTop + el.clientHeight >= el.scrollHeight - 100 && !loading.value) {
    loadAlerts()
  }
}

let timer = null
onMounted(() => {
  loadAll()
  window.addEventListener('scroll', onScroll, { passive: true })
  // 值班监控：30s 自动刷新状态与首页列表
  timer = setInterval(() => {
    loadSummary()
    if (activeTab.value === 'terminals') {
      if (page.value <= 1 && !loading.value) loadTerminals(true)
    } else if (alertPage.value <= 1 && !loading.value) {
      loadAlerts(true)
    }
  }, 30000)
})
onUnmounted(() => {
  window.removeEventListener('scroll', onScroll)
  if (timer) clearInterval(timer)
})
</script>
