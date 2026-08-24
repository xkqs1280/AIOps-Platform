<template>
  <div class="p-4">
    <header class="mb-3 flex items-center justify-between">
      <h1 class="text-lg font-bold text-ink-strong">设备管理</h1>
      <span class="text-xs text-ink-faint">共 {{ total }} 台</span>
    </header>

    <!-- 搜索 -->
    <div class="mb-3 flex gap-2">
      <input
        v-model="keyword"
        type="text"
        placeholder="搜索设备名 / IP"
        class="flex-1 rounded-xl border border-line bg-surface px-3 py-2.5 text-sm text-ink-strong outline-none placeholder:text-ink-faint focus:border-cyan-500"
      />
      <button @click="load(true)" class="rounded-xl bg-cyan-600 px-4 text-sm font-medium text-white active:bg-cyan-500">搜索</button>
    </div>

    <!-- 状态筛选 -->
    <div class="mb-3 flex gap-2">
      <button
        v-for="f in filters"
        :key="f.value"
        @click="statusFilter = f.value; load(true)"
        class="rounded-full px-3 py-1 text-xs"
        :class="statusFilter === f.value ? 'bg-cyan-600 text-white' : 'border border-line text-ink-muted'"
      >
        {{ f.label }}
      </button>
    </div>

    <!-- 设备列表 -->
    <div v-if="devices.length" class="space-y-2.5">
      <div
        v-for="d in devices"
        :key="d.id"
        @click="$router.push(`/devices/${d.id}`)"
        class="flex items-center gap-3 rounded-2xl border border-line bg-surface p-3.5 active:bg-surface-2"
      >
        <span class="h-2.5 w-2.5 shrink-0 rounded-full" :class="statusDot(d.status)"></span>
        <div class="min-w-0 flex-1">
          <p class="truncate text-sm font-medium text-ink-strong">{{ d.name }}</p>
          <p class="truncate text-xs text-ink-faint">{{ d.ip }} · {{ d.model || d.vendor || '—' }}</p>
        </div>
        <span class="shrink-0 text-xs" :class="statusTextColor(d.status)">{{ statusText(d.status) }}</span>
      </div>
      <p v-if="hasMore" class="py-2 text-center text-xs text-ink-faint">上滑加载更多</p>
    </div>
    <div v-else class="py-16 text-center text-sm text-ink-faint">{{ loading ? '加载中...' : '暂无设备' }}</div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { getDevices } from '../api.js'

const devices = ref([])
const total = ref(0)
const keyword = ref('')
const statusFilter = ref('')
const page = ref(1)
const hasMore = ref(false)
const loading = ref(false)

const filters = [
  { label: '全部', value: '' },
  { label: '在线', value: 'online' },
  { label: '告警', value: 'warning' },
  { label: '离线', value: 'offline' },
]

const statusDot = (s) => ({ online: 'bg-emerald-400', warning: 'bg-amber-400', offline: 'bg-red-400' }[s] || 'bg-ink-faint')
const statusText = (s) => ({ online: '在线', warning: '告警', offline: '离线' }[s] || s || '—')
const statusTextColor = (s) => ({ online: 'text-emerald-400', warning: 'text-amber-400', offline: 'text-red-400' }[s] || 'text-ink-muted')

async function load(reset = false) {
  if (loading.value) return
  if (reset) { page.value = 1; devices.value = [] }
  loading.value = true
  try {
    const res = await getDevices({
      page: page.value,
      page_size: 20,
      keyword: keyword.value || undefined,
      status: statusFilter.value || undefined,
    })
    const items = res?.items || res?.data || []
    total.value = res?.total ?? items.length
    devices.value = reset ? items : [...devices.value, ...items]
    hasMore.value = page.value * 20 < total.value
    if (hasMore.value) page.value += 1
  } catch (e) {
    console.error(e)
  } finally {
    loading.value = false
  }
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
