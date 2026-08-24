<template>
  <div class="p-4 pb-8">
    <!-- 返回 -->
    <button @click="$router.back()" class="mb-3 flex items-center gap-1 text-sm text-ink-muted active:text-ink">← 返回</button>

    <div v-if="device" class="space-y-4">
      <!-- 设备头部 -->
      <div class="rounded-2xl border border-line bg-surface p-4">
        <div class="mb-3 flex items-center justify-between">
          <div class="flex items-center gap-3">
            <span class="h-3 w-3 rounded-full" :class="statusDot"></span>
            <div>
              <h1 class="text-lg font-bold text-ink-strong">{{ device.name }}</h1>
              <p class="text-xs text-ink-faint">{{ device.ip }}</p>
            </div>
          </div>
          <button @click="doSync" :disabled="syncing" class="rounded-lg border border-line px-3 py-1.5 text-xs text-ink-muted active:bg-surface-2">
            {{ syncing ? '同步中...' : '重新同步' }}
          </button>
        </div>
        <div class="grid grid-cols-3 gap-2 text-center">
          <div class="rounded-xl bg-surface-2/60 py-2.5">
            <p class="text-base font-bold" :class="cpuColor">{{ device.cpu_usage ?? '—' }}%</p>
            <p class="text-[10px] text-ink-faint">CPU</p>
          </div>
          <div class="rounded-xl bg-surface-2/60 py-2.5">
            <p class="text-base font-bold" :class="memColor">{{ device.memory_usage ?? '—' }}%</p>
            <p class="text-[10px] text-ink-faint">内存</p>
          </div>
          <div class="rounded-xl bg-surface-2/60 py-2.5">
            <p class="text-base font-bold text-ink">{{ statusText }}</p>
            <p class="text-[10px] text-ink-faint">状态</p>
          </div>
        </div>
      </div>

      <!-- 基本信息 -->
      <div class="rounded-2xl border border-line bg-surface p-4">
        <p class="mb-2 text-sm font-semibold text-ink">基本信息</p>
        <div class="space-y-1.5 text-sm">
          <div class="flex justify-between"><span class="text-ink-faint">厂商</span><span class="text-ink">{{ device.vendor || '—' }}</span></div>
          <div class="flex justify-between"><span class="text-ink-faint">型号</span><span class="text-ink">{{ device.model || '—' }}</span></div>
          <div class="flex justify-between"><span class="text-ink-faint">类型</span><span class="text-ink">{{ device.device_type || '—' }}</span></div>
          <div class="flex justify-between"><span class="text-ink-faint">所属分组</span><span class="text-ink">{{ device.group_name || '—' }}</span></div>
          <div class="flex justify-between"><span class="text-ink-faint">SNMP 版本</span><span class="text-ink">{{ device.snmp_version || '—' }}</span></div>
        </div>
      </div>

      <!-- 设备告警 -->
      <div class="rounded-2xl border border-line bg-surface">
        <div class="flex items-center justify-between border-b border-line px-4 py-3">
          <span class="text-sm font-semibold text-ink">设备告警</span>
          <span class="text-xs text-ink-faint">{{ alerts.length }} 条</span>
        </div>
        <div v-if="alerts.length === 0" class="px-4 py-5 text-center text-xs text-ink-faint">暂无告警</div>
        <div v-for="a in alerts" :key="a.id" class="flex items-center gap-2 border-b border-line/60 px-4 py-2.5 last:border-0">
          <span class="h-1.5 w-1.5 shrink-0 rounded-full" :class="sevDot(a.severity)"></span>
          <span class="truncate text-sm text-ink-muted">{{ a.message || a.alert_name }}</span>
          <span class="ml-auto shrink-0 text-[10px]" :class="sevText(a.severity)">{{ a.severity }}</span>
        </div>
      </div>
    </div>

    <div v-else-if="error" class="py-16 text-center text-sm text-red-400">{{ error }}</div>
    <div v-else class="py-16 text-center text-sm text-ink-faint">加载中...</div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { getDevice, getAlerts, syncDevice } from '../api.js'

const route = useRoute()
const deviceId = route.params.id
const device = ref(null)
const alerts = ref([])
const error = ref('')
const syncing = ref(false)

const statusDot = computed(() => ({ online: 'bg-emerald-400', warning: 'bg-amber-400', offline: 'bg-red-400' }[device.value?.status] || 'bg-ink-faint'))
const statusText = computed(() => ({ online: '在线', warning: '告警', offline: '离线' }[device.value?.status] || device.value?.status || '—'))
const cpuColor = computed(() => (device.value?.cpu_usage >= 80 ? 'text-red-400' : device.value?.cpu_usage >= 60 ? 'text-amber-400' : 'text-emerald-400'))
const memColor = computed(() => (device.value?.memory_usage >= 80 ? 'text-red-400' : device.value?.memory_usage >= 60 ? 'text-amber-400' : 'text-emerald-400'))
const sevDot = (s) => ({ critical: 'bg-red-500', major: 'bg-amber-500', minor: 'bg-yellow-500', warning: 'bg-cyan-400' }[s] || 'bg-ink-faint')
const sevText = (s) => ({ critical: 'text-red-400', major: 'text-amber-400', minor: 'text-yellow-400', warning: 'text-cyan-400' }[s] || 'text-ink-muted')

async function load() {
  try {
    const [d, a] = await Promise.all([
      getDevice(deviceId).catch(() => null),
      getAlerts({ device_id: deviceId, page_size: 8 }).catch(() => ({ items: [] })),
    ])
    device.value = d
    alerts.value = a?.items || a || []
    if (!d) error.value = '设备不存在或无法访问'
  } catch (e) {
    error.value = e?.response?.data?.detail || '加载失败'
  }
}

async function doSync() {
  if (syncing.value) return
  syncing.value = true
  try { await syncDevice(deviceId); await load() } catch (e) { error.value = '同步失败' }
  finally { syncing.value = false }
}

onMounted(load)
</script>
