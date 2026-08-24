<template>
  <div class="p-4 pb-8">
    <button @click="$router.back()" class="mb-3 flex items-center gap-1 text-sm text-slate-400 active:text-slate-200">← 返回</button>
    <h1 class="mb-3 text-lg font-bold text-slate-100">等保合规</h1>

    <div v-if="summary" class="mb-4 rounded-2xl border border-slate-800 bg-slate-900 p-4">
      <div class="mb-2 flex items-center justify-between">
        <span class="text-xs text-slate-500">整体合规评分</span>
        <span class="text-lg font-bold" :class="scoreColor">{{ summary.score ?? summary.overall_score ?? '—' }}</span>
      </div>
      <div class="h-2 overflow-hidden rounded-full bg-slate-800">
        <div class="h-full rounded-full" :style="{ width: (summary.score ?? summary.overall_score ?? 0) + '%', background: scoreBarColor }"></div>
      </div>
      <div class="mt-3 flex gap-3 text-center">
        <div class="flex-1 rounded-xl bg-slate-800/60 py-2">
          <p class="text-sm font-bold text-slate-100">{{ summary.passed ?? summary.pass_count ?? 0 }}</p>
          <p class="text-[10px] text-slate-500">通过</p>
        </div>
        <div class="flex-1 rounded-xl bg-slate-800/60 py-2">
          <p class="text-sm font-bold text-amber-400">{{ summary.warnings ?? summary.warning_count ?? 0 }}</p>
          <p class="text-[10px] text-slate-500">警告</p>
        </div>
        <div class="flex-1 rounded-xl bg-slate-800/60 py-2">
          <p class="text-sm font-bold text-red-400">{{ summary.failed ?? summary.fail_count ?? 0 }}</p>
          <p class="text-[10px] text-slate-500">不通过</p>
        </div>
      </div>
    </div>

    <div v-if="items.length" class="space-y-2">
      <div v-for="it in items" :key="it.id ?? it.check_item" class="flex items-center gap-3 rounded-2xl border border-slate-800 bg-slate-900 p-3">
        <span class="text-lg">{{ statusIcon(it.status ?? it.result) }}</span>
        <div class="min-w-0 flex-1">
          <p class="truncate text-sm text-slate-200">{{ it.check_item || it.item || it.name }}</p>
          <p class="text-xs text-slate-500">{{ it.device_name || '' }}</p>
        </div>
      </div>
    </div>
    <div v-else-if="!loading" class="py-16 text-center text-sm text-slate-600">暂无合规数据</div>
    <div v-else class="py-16 text-center text-sm text-slate-600">加载中...</div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { getComplianceStatus } from '../api.js'

const summary = ref(null)
const items = ref([])
const loading = ref(false)

const scoreColor = computed(() => ((summary.value?.score ?? summary.value?.overall_score ?? 0) >= 80 ? 'text-emerald-400' : (summary.value?.score ?? 0) >= 60 ? 'text-amber-400' : 'text-red-400'))
const scoreBarColor = computed(() => { const s = summary.value?.score ?? summary.value?.overall_score ?? 0; return s >= 80 ? '#10b981' : s >= 60 ? '#f59e0b' : '#ef4444' })

const statusIcon = (s) => ({ passed: '✅', pass: '✅', warning: '⚠️', failed: '❌', fail: '❌' }[s] || '➖')

onMounted(async () => {
  loading.value = true
  try {
    const res = await getComplianceStatus()
    const data = res?.data || res || {}
    summary.value = data.summary || data.overview || data
    items.value = data.items || data.results || data.details || []
  } catch (e) { console.error(e) }
  finally { loading.value = false }
})
</script>
