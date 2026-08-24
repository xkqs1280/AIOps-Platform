<template>
  <div class="p-4 pb-8">
    <button @click="$router.back()" class="mb-3 flex items-center gap-1 text-sm text-slate-400 active:text-slate-200">← 返回</button>
    <h1 class="mb-3 text-lg font-bold text-slate-100">生命周期管理</h1>

    <div v-if="items.length" class="space-y-2.5">
      <div v-for="it in items" :key="it.id" class="rounded-2xl border border-slate-800 bg-slate-900 p-3.5">
        <div class="flex items-center justify-between">
          <p class="text-sm font-medium text-slate-100">{{ it.device_name || it.name || '设备' }}</p>
          <span class="rounded px-1.5 py-0.5 text-[10px]" :class="typeBadge(it.reminder_type || it.type)">{{ it.reminder_type || it.type || '—' }}</span>
        </div>
        <p class="mt-1.5 text-xs text-slate-400">{{ it.description || it.message || '' }}</p>
        <p v-if="it.expire_at || it.expire_date" class="mt-1 text-xs" :class="expiringSoon(it) ? 'text-amber-400' : 'text-slate-500'">
          到期：{{ it.expire_at || it.expire_date }} {{ expiringSoon(it) ? '⚠ 即将到期' : '' }}
        </p>
      </div>
    </div>
    <div v-else class="py-16 text-center text-sm text-slate-600">{{ loading ? '加载中...' : '暂无提醒' }}</div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getLifecycleReminders } from '../api.js'

const items = ref([])
const loading = ref(false)

const typeBadge = (t) => ({ warranty: 'bg-cyan-500/15 text-cyan-400', support: 'bg-amber-500/15 text-amber-400', license: 'bg-emerald-500/15 text-emerald-400' }[t] || 'bg-slate-500/15 text-slate-400')

function expiringSoon(it) {
  const d = it.expire_at || it.expire_date
  if (!d) return false
  const days = Math.ceil((new Date(d) - new Date()) / 86400000)
  return days >= 0 && days <= 30
}

onMounted(async () => {
  loading.value = true
  try {
    const res = await getLifecycleReminders()
    items.value = res?.items || res?.reminders || res?.data || (Array.isArray(res) ? res : [])
  } catch (e) { console.error(e) }
  finally { loading.value = false }
})
</script>
