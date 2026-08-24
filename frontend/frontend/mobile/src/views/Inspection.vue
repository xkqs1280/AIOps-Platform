<template>
  <div class="p-4 pb-8">
    <button @click="$router.back()" class="mb-3 flex items-center gap-1 text-sm text-slate-400 active:text-slate-200">← 返回</button>
    <h1 class="mb-3 text-lg font-bold text-slate-100">设备巡检</h1>

    <div v-if="tasks.length" class="space-y-2.5">
      <div v-for="t in tasks" :key="t.id" class="rounded-2xl border border-slate-800 bg-slate-900 p-3.5">
        <div class="flex items-center justify-between">
          <p class="text-sm font-medium text-slate-100">{{ t.task_name || t.name || '巡检任务' }}</p>
          <span class="rounded px-1.5 py-0.5 text-[10px]" :class="statusBadge(t.status)">{{ t.status || '—' }}</span>
        </div>
        <p class="mt-1 text-xs text-slate-500">{{ t.device_name || '' }}</p>
        <div v-if="t.score != null" class="mt-2 flex items-center gap-2">
          <div class="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-800">
            <div class="h-full rounded-full" :style="{ width: t.score + '%', background: scoreColor(t.score) }"></div>
          </div>
          <span class="text-xs font-semibold" :style="{ color: scoreColor(t.score) }">{{ t.score }}</span>
        </div>
        <p class="mt-1.5 text-xs text-slate-600">{{ fmtTime(t.created_at || t.start_time) }}</p>
      </div>
    </div>
    <div v-else class="py-16 text-center text-sm text-slate-600">{{ loading ? '加载中...' : '暂无巡检任务' }}</div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getInspectionTasks } from '../api.js'

const tasks = ref([])
const loading = ref(false)

const statusBadge = (s) => ({ completed: 'bg-emerald-500/15 text-emerald-400', running: 'bg-cyan-500/15 text-cyan-400', failed: 'bg-red-500/15 text-red-400', pending: 'bg-amber-500/15 text-amber-400' }[s] || 'bg-slate-500/15 text-slate-400')
const scoreColor = (s) => (s >= 90 ? '#10b981' : s >= 70 ? '#f59e0b' : '#ef4444')
const fmtTime = (t) => {
  if (!t) return ''
  const d = new Date(t)
  if (isNaN(d)) return String(t).slice(0, 16).replace('T', ' ')
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

onMounted(async () => {
  loading.value = true
  try {
    const res = await getInspectionTasks({ page_size: 20 })
    tasks.value = res?.items || res?.data || (Array.isArray(res) ? res : [])
  } catch (e) { console.error(e) }
  finally { loading.value = false }
})
</script>
