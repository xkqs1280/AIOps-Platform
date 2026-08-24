<template>
  <div class="p-4 pb-8">
    <button @click="$router.back()" class="mb-3 flex items-center gap-1 text-sm text-ink-muted active:text-ink">← 返回</button>
    <h1 class="mb-3 text-lg font-bold text-ink-strong">安全监控</h1>

    <div v-if="latest" class="mb-4 rounded-2xl border border-line bg-surface p-4">
      <p class="mb-1 text-xs text-ink-faint">最新外部威胁</p>
      <p class="text-sm font-medium text-ink-strong">{{ latest.title || latest.name || '威胁态势' }}</p>
      <p class="mt-1 text-xs text-ink-muted">{{ latest.summary || latest.description || '' }}</p>
      <p class="mt-2 text-[10px] text-ink-faint">{{ fmtTime(latest.timestamp || latest.created_at) }}</p>
    </div>

    <div v-if="history.length" class="space-y-2">
      <div v-for="h in history" :key="h.id ?? h.timestamp" class="rounded-2xl border border-line bg-surface p-3">
        <div class="flex items-center justify-between">
          <p class="truncate text-sm text-ink">{{ h.title || h.name || '威胁事件' }}</p>
          <span class="shrink-0 rounded px-1.5 py-0.5 text-[10px]" :class="levelBadge(h.level ?? h.severity)">{{ h.level ?? h.severity ?? 'info' }}</span>
        </div>
        <p class="mt-1 text-xs text-ink-faint">{{ fmtTime(h.timestamp || h.created_at) }}</p>
      </div>
    </div>
    <div v-else-if="!loading" class="py-16 text-center text-sm text-ink-faint">暂无威胁数据</div>
    <div v-else class="py-16 text-center text-sm text-ink-faint">加载中...</div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getExternalThreatLatest, getExternalThreatHistory } from '../api.js'

const latest = ref(null)
const history = ref([])
const loading = ref(false)

const levelBadge = (l) => ({ critical: 'bg-red-500/15 text-red-400', high: 'bg-orange-500/15 text-orange-400', medium: 'bg-amber-500/15 text-amber-400', low: 'bg-ink-faint/15 text-ink-muted' }[l] || 'bg-ink-faint/15 text-ink-muted')
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
    const [l, h] = await Promise.all([
      getExternalThreatLatest().catch(() => null),
      getExternalThreatHistory({ limit: 20 }).catch(() => []),
    ])
    latest.value = l?.data || l
    history.value = Array.isArray(h) ? h : (h?.items || h?.data || [])
  } catch (e) { console.error(e) }
  finally { loading.value = false }
})
</script>
