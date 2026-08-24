<script setup>
import { ref, computed, onMounted, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import * as echarts from 'echarts'
import { getDevice, getAlerts, updateDevice, getMetricHistory, syncDevice, getDeviceInterfaces, getDeviceComponents } from '../api/index.js'

const route = useRoute()
const router = useRouter()
const deviceId = computed(() => route.params.id)

const device = ref(null)
const alerts = ref([])
const loading = ref(true)
const error = ref(null)
const isEditing = ref(false)
const saving = ref(false)
const editError = ref(null)

const editForm = ref({
  name: '',
  ip: '',
  vendor: '',
  model: '',
  serial_number: '',
  snmp_version: '',
  snmp_community: '',
  mgmt_protocol: 'ssh',
  mgmt_port: 22,
  mgmt_username: '',
  mgmt_password: '',
  device_type: '',
  group_name: '',
  location: '',  status: ''
})

// 切换管理协议时自动设置默认端口（SSH->22，Telnet->23）
function onProtocolChange() {
  if (editForm.value.mgmt_protocol === 'ssh') editForm.value.mgmt_port = 22
  else if (editForm.value.mgmt_protocol === 'telnet') editForm.value.mgmt_port = 23
}

function formatDate(dateStr) {
  if (!dateStr) return 'N/A'
  return new Date(dateStr).toLocaleDateString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit'
  })
}

function formatDateTime(dateStr) {
  if (!dateStr) return '-'
  return new Date(dateStr).toLocaleString('zh-CN')
}

// 凭据掩码：只显示首尾，避免明文泄露（SNMP community 等）
function maskSecret(v) {
  if (!v) return '-'
  const s = String(v)
  if (s.length <= 4) return '****'
  return s.slice(0, 2) + '****' + s.slice(-2)
}

function statusColor(status) {
  const map = { online: 'bg-green-500', offline: 'bg-red-500', warning: 'bg-yellow-500', unknown: 'bg-gray-500' }
  return map[status?.toLowerCase()] || 'bg-gray-500'
}

function severityColor(severity) {
  const map = { critical: 'bg-red-600', major: 'bg-orange-500', minor: 'bg-yellow-500', warning: 'bg-blue-500' }
  return map[severity?.toLowerCase()] || 'bg-gray-500'
}

function severityTextColor(severity) {
  const map = { critical: 'text-red-400', major: 'text-orange-400', minor: 'text-yellow-400', warning: 'text-blue-400' }
  return map[severity?.toLowerCase()] || 'text-gray-400'
}

function severityLabel(severity) {
  const map = { critical: '严重', major: '重要', minor: '次要', warning: '警告' }
  return map[severity?.toLowerCase()] || severity
}

function statusLabel(status) {
  const map = { online: '在线', offline: '离线', warning: '告警', unknown: '未知' }
  return map[status] || status
}

function mgmtProtocolLabel(protocol) {
  const map = { ssh: 'SSH', telnet: 'Telnet' }
  return map[protocol] || protocol || '-'
}

function deviceTypeLabel(type) {
  const map = { router: '路由器', switch: '交换机', firewall: '防火墙', load_balancer: '负载均衡', server: '服务器', wireless: '无线控制器' }
  return map[type] || type || '-'
}

function alertStatusColor(status) {
  return status === 'active' ? 'bg-red-500' : 'bg-green-600'
}

function gaugeColor(value, thresholds = [50, 80]) {
  if (value >= thresholds[1]) return '#ef4444'
  if (value >= thresholds[0]) return '#f59e0b'
  return '#22c55e'
}

function tempGaugeColor(value) {
  if (value >= 75) return '#ef4444'
  if (value >= 55) return '#f59e0b'
  return '#22c55e'
}

const cpuColor = computed(() => gaugeColor(device.value?.cpu_usage ?? 0))
const memColor = computed(() => gaugeColor(device.value?.memory_usage ?? 0))
const tempColor = computed(() => tempGaugeColor(device.value?.temperature ?? 0))

// 内存历史趋势图（真实 SNMP 采集）
const memChartRef = ref(null)
let memChart = null
const metricsNote = ref('加载中...')

async function loadMemoryHistory() {
  if (!deviceId.value) return
  try {
    const res = await getMetricHistory({ device_id: deviceId.value, metric_type: 'memory', hours: 24 })
    const data = (res.data || []).filter(d => d.value != null)
    if (data.length === 0) {
      metricsNote.value = '内存指标采集中，约 1 分钟后出现首个真实数据点'
      return
    }
    renderMemoryChart(data)
    metricsNote.value = `已采集 ${data.length} 个真实数据点（近 24 小时）`
  } catch (e) {
    metricsNote.value = '历史数据加载失败'
  }
}

function renderMemoryChart(data) {
  if (!memChartRef.value) return
  if (!memChart) memChart = echarts.init(memChartRef.value)
  const times = data.map(d => new Date(d.recorded_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }))
  const vals = data.map(d => d.value)
  memChart.setOption({
    grid: { left: 44, right: 16, top: 20, bottom: 28 },
    tooltip: { trigger: 'axis', formatter: '{b}<br/>内存: {c}%' },
    xAxis: {
      type: 'category', data: times, boundaryGap: false,
      axisLine: { lineStyle: { color: '#4b5563' } },
      axisLabel: { color: '#9ca3af', fontSize: 10 }
    },
    yAxis: {
      type: 'value', max: 100, min: 0,
      axisLabel: { color: '#9ca3af', fontSize: 10, formatter: '{value}%' },
      splitLine: { lineStyle: { color: '#1f2937' } }
    },
    series: [{
      type: 'line', data: vals, smooth: true, showSymbol: false,
      lineStyle: { color: '#3b82f6', width: 2 },
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(59,130,246,0.35)' },
          { offset: 1, color: 'rgba(59,130,246,0.02)' }
        ])
      }
    }]
  })
  memChart.resize()
}

const lifecycleStages = computed(() => {
  if (!device.value) return []
  const now = new Date()
  const stages = []
  const fields = [
    { key: 'warranty_expire', label: '保修到期', color: 'bg-blue-500' },
    { key: 'eos_date', label: '维保到期', color: 'bg-yellow-500' },
    { key: 'eol_date', label: '寿命到期', color: 'bg-red-500' }
  ]

  const dates = fields
    .map(f => ({ ...f, date: device.value[f.key] ? new Date(device.value[f.key]) : null }))
    .filter(f => f.date)

  if (dates.length === 0) return []

  const allDates = dates.map(d => d.date)
  const min = new Date(Math.min(now, ...allDates))
  const max = new Date(Math.max(...allDates))
  const range = max - min || 1

  return dates.map(d => ({
    ...d,
    position: ((d.date - min) / range) * 100,
    isPast: d.date < now
  })).concat(
    min <= now && now <= max
      ? [{ label: '今天', position: ((now - min) / range) * 100, color: 'bg-gray-400', isPast: false, isNow: true }]
      : []
  )
})

async function fetchDevice() {
  loading.value = true
  error.value = null
  try {
    const [deviceData, alertData] = await Promise.all([
      getDevice(deviceId.value),
      getAlerts({ device_id: deviceId.value })
    ])
    device.value = deviceData
    alerts.value = (alertData.items || []).slice(0, 10)
    // 异步加载接口流量（双采样约需十几秒，不阻塞页面）
    loadInterfaces()
    // 异步加载硬件组件明细（板卡序列号/型号等）
    loadComponents()
  } catch (err) {
    error.value = err.message || '加载设备信息失败'
  } finally {
    loading.value = false
  }
}

// === 接口流量 TOP10（实时 SNMP）===
const interfaces = ref([])
const ifLoading = ref(false)
const ifError = ref('')

async function loadInterfaces() {
  if (!deviceId.value) return
  ifLoading.value = true
  ifError.value = ''
  try {
    const data = await getDeviceInterfaces(deviceId.value, 10)
    interfaces.value = data.interfaces || []
  } catch (e) {
    ifError.value = e.response?.data?.detail || e.message || '接口流量采集失败'
    interfaces.value = []
  } finally {
    ifLoading.value = false
  }
}

// === 硬件组件明细（实体 MIB 采集）===
const components = ref([])
const compLoading = ref(false)
const compError = ref('')

async function loadComponents() {
  if (!deviceId.value) return
  compLoading.value = true
  compError.value = ''
  try {
    const data = await getDeviceComponents(deviceId.value)
    components.value = data || []
  } catch (e) {
    compError.value = e.response?.data?.detail || e.message || '组件信息加载失败'
    components.value = []
  } finally {
    compLoading.value = false
  }
}

function formatBw(bps) {
  if (!bps) return '-'
  if (bps >= 1e9) return (bps / 1e9).toFixed(1) + 'G'
  if (bps >= 1e6) return Math.round(bps / 1e6) + 'M'
  if (bps >= 1e3) return Math.round(bps / 1e3) + 'K'
  return bps + 'B'
}

function formatRate(bps) {
  if (!bps) return '0'
  if (bps >= 1e9) return (bps / 1e9).toFixed(2) + 'G'
  if (bps >= 1e6) return (bps / 1e6).toFixed(2) + 'M'
  if (bps >= 1e3) return (bps / 1e3).toFixed(2) + 'K'
  return bps + ''
}

// 从设备 SNMP 重新发现并同步信息（覆盖名称/型号/厂商等），解决设备改名后平台不更新
const syncing = ref(false)
async function syncInfo() {
  if (syncing.value) return
  syncing.value = true
  try {
    const updated = await syncDevice(deviceId.value)
    device.value = { ...device.value, ...updated }
    alert(`同步完成：名称=${updated.name}，型号=${updated.model || '-'}`)
    // 同步会刷新实体 MIB 组件明细，重新加载
    loadComponents()
  } catch (err) {
    alert(`同步失败：${err.response?.data?.detail || err.message}`)
  } finally {
    syncing.value = false
  }
}

function startEdit() {
  editForm.value = {
    name: device.value.name || '',
    ip: device.value.ip || '',
    vendor: device.value.vendor || '',
    model: device.value.model || '',
    serial_number: device.value.serial_number || '',
    snmp_version: device.value.snmp_version || '',
    snmp_community: device.value.snmp_community || '',
    mgmt_protocol: device.value.mgmt_protocol || 'ssh',
    mgmt_port: device.value.mgmt_port || 22,
    mgmt_username: device.value.mgmt_username || '',
    mgmt_password: device.value.mgmt_password || '',
    device_type: device.value.device_type || '',
    group_name: device.value.group_name || '',
    location: device.value.location || '',
    status: device.value.status || ''
  }
  editError.value = null
  isEditing.value = true
}

function cancelEdit() {
  isEditing.value = false
  editError.value = null
}

async function saveDevice() {
  saving.value = true
  editError.value = null
  try {
    const updated = await updateDevice(deviceId.value, editForm.value)
    device.value = updated
    isEditing.value = false
  } catch (err) {
    editError.value = err.message || '保存失败'
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  fetchDevice()
  loadMemoryHistory()
})

// 设备切换时重新加载全部数据（基本信息/告警/接口流量/历史）
watch(deviceId, () => {
  nextTick(() => {
    fetchDevice()
    loadMemoryHistory()
  })
})
</script>

<template>
  <div class="min-h-screen bg-gray-950 text-gray-100">
    <!-- Loading -->
    <div v-if="loading" class="p-6 space-y-6 animate-pulse">
      <div class="h-8 w-48 bg-gray-800 rounded" />
      <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div class="bg-gray-900 border border-gray-800 rounded-xl p-6 space-y-4">
          <div v-for="i in 10" :key="i" class="h-5 bg-gray-800 rounded" :style="{ width: `${60 + Math.random() * 30}%` }" />
        </div>
      </div>
      <div class="grid grid-cols-3 gap-6">
        <div v-for="i in 3" :key="i" class="bg-gray-900 border border-gray-800 rounded-xl p-6 h-48" />
      </div>
    </div>

    <!-- Error -->
    <div v-else-if="error" class="p-6 flex flex-col items-center justify-center min-h-[60vh]">
      <div class="text-red-400 text-lg mb-4">{{ error }}</div>
      <button
        @click="fetchDevice"
        class="px-4 py-2 bg-gray-800 hover:bg-gray-700 text-gray-200 rounded-lg border border-gray-700 transition-colors"
      >
        重试
      </button>
    </div>

    <!-- Main Content -->
    <template v-else-if="device">
      <!-- Header -->
      <div class="sticky top-0 z-10 bg-gray-950/95 backdrop-blur border-b border-gray-800 px-6 py-4">
        <div class="flex items-center justify-between max-w-7xl mx-auto">
          <div class="flex items-center gap-4">
            <button
              @click="router.push('/devices')"
              class="flex items-center gap-1 text-gray-400 hover:text-gray-200 transition-colors"
            >
              <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" />
              </svg>
              <span>返回设备列表</span>
            </button>
            <h1 class="text-xl font-bold">{{ device.name }}</h1>
            <span class="px-2.5 py-0.5 rounded-full text-xs font-medium" :class="statusColor(device.status)">
              {{ statusLabel(device.status) }}
            </span>
          </div>
          <div class="flex items-center gap-2">
            <button
              v-if="!isEditing"
              @click="syncInfo"
              :disabled="syncing"
              class="px-4 py-2 bg-cyan-600 hover:bg-cyan-700 disabled:opacity-50 text-white rounded-lg transition-colors font-medium text-sm"
            >
              {{ syncing ? '同步中…' : '同步信息' }}
            </button>
            <button
              v-if="!isEditing"
              @click="startEdit"
              class="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg transition-colors font-medium text-sm"
            >
              编辑设备
            </button>
          </div>
        </div>
      </div>

      <div class="max-w-7xl mx-auto p-6 space-y-6">
        <!-- Edit Error Banner -->
        <div
          v-if="editError"
          class="bg-red-900/30 border border-red-700 rounded-lg p-3 text-red-300 text-sm flex items-center justify-between"
        >
          <span>{{ editError }}</span>
          <button @click="editError = null" class="text-red-400 hover:text-red-300">&times;</button>
        </div>

        <!-- Device Info Card -->
        <div class="bg-gray-900 border border-gray-800 rounded-xl p-6">
          <h2 class="text-lg font-semibold mb-4 text-gray-200">设备信息</h2>

          <!-- View Mode -->
          <div v-if="!isEditing" class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-x-8 gap-y-4">
            <div v-for="field in [
              { label: '设备名称', value: device.name },
              { label: 'IP 地址', value: device.ip },
              { label: '厂商', value: device.vendor },
              { label: '型号', value: device.model },
              { label: '序列号', value: device.serial_number },
              { label: 'SNMP 版本', value: device.snmp_version },
              { label: 'SNMP Community', value: device.snmp_community ? maskSecret(device.snmp_community) : '-' },
              { label: '管理协议', value: mgmtProtocolLabel(device.mgmt_protocol) },
              { label: '管理端口', value: device.mgmt_port },
              { label: '管理用户名', value: device.mgmt_username },
              { label: '设备类型', value: deviceTypeLabel(device.device_type) },
              { label: '所属分组', value: device.group_name },
              { label: '位置', value: device.location },
              { label: '最后上线', value: formatDateTime(device.last_seen) },
              { label: '创建时间', value: formatDateTime(device.created_at) }
            ]" :key="field.label" class="min-w-0">
              <div class="text-xs text-gray-500 mb-0.5">{{ field.label }}</div>
              <div class="text-sm text-gray-200 truncate" :title="field.value || '-'">
                {{ field.value || '-' }}
              </div>
            </div>
          </div>

          <!-- Edit Mode Form -->
          <form v-else @submit.prevent="saveDevice" class="space-y-6">
            <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              <div v-for="field in [
                { key: 'name', label: '设备名称', type: 'text' },
                { key: 'ip', label: 'IP 地址', type: 'text' },
                { key: 'vendor', label: '厂商', type: 'text' },
                { key: 'model', label: '型号', type: 'text' },
                { key: 'serial_number', label: '序列号', type: 'text' },
                { key: 'snmp_version', label: 'SNMP 版本', type: 'text' },
                { key: 'snmp_community', label: 'SNMP Community', type: 'text' },
                { key: 'device_type', label: '设备类型', type: 'text' },
                { key: 'group_name', label: '所属分组', type: 'text' },
                { key: 'location', label: '位置', type: 'text' }
              ]" :key="field.key">
                <label class="block text-xs text-gray-400 mb-1">{{ field.label }}</label>
                <input
                  v-model="editForm[field.key]"
                  :type="field.type"
                  class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200
                         focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors
                         placeholder-gray-600"
                />
              </div>
              <div>
                <label class="block text-xs text-gray-400 mb-1">状态</label>
                <select
                  v-model="editForm.status"
                  class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200
                         focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors"
                >
                  <option value="online">在线</option>
                  <option value="offline">离线</option>
                  <option value="warning">告警</option>
                  <option value="unknown">未知</option>
                </select>
              </div>
            </div>

            <!-- 远程管理配置 -->
            <div class="border-t border-gray-800 pt-4">
              <div class="text-sm text-gray-400 mb-3 font-medium">远程管理配置</div>
              <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                <div>
                  <label class="block text-xs text-gray-400 mb-1">管理协议</label>
                  <select
                    v-model="editForm.mgmt_protocol"
                    @change="onProtocolChange"
                    class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200
                           focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors"
                  >
                    <option value="ssh">SSH</option>
                    <option value="telnet">Telnet</option>
                  </select>
                </div>
                <div>
                  <label class="block text-xs text-gray-400 mb-1">端口</label>
                  <input
                    v-model.number="editForm.mgmt_port"
                    type="number"
                    class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200
                           focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors"
                  />
                </div>
                <div>
                  <label class="block text-xs text-gray-400 mb-1">用户名</label>
                  <input
                    v-model="editForm.mgmt_username"
                    type="text"
                    class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200
                           focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors
                           placeholder-gray-600"
                  />
                </div>
                <div>
                  <label class="block text-xs text-gray-400 mb-1">密码</label>
                  <input
                    v-model="editForm.mgmt_password"
                    type="password"
                    class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200
                           focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors
                           placeholder-gray-600"
                  />
                </div>
              </div>
            </div>
            <div class="flex items-center gap-3 pt-2 border-t border-gray-800">
              <button
                type="submit"
                :disabled="saving"
                class="px-5 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed
                       text-white rounded-lg transition-colors font-medium text-sm"
              >
                {{ saving ? '保存中...' : '保存修改' }}
              </button>
              <button
                type="button"
                @click="cancelEdit"
                class="px-5 py-2 bg-gray-700 hover:bg-gray-600 text-gray-200 rounded-lg transition-colors text-sm"
              >
                取消
              </button>
            </div>
          </form>
        </div>

        <!-- 硬件组件明细（实体 MIB 采集：板卡/电源/风扇等） -->
        <div class="bg-gray-900 border border-gray-800 rounded-xl p-6 mt-6">
          <div class="flex items-center justify-between mb-4">
            <h2 class="text-lg font-semibold text-gray-200">硬件组件明细</h2>
            <button
              class="px-3 py-1.5 text-xs rounded-lg bg-gray-800 border border-gray-700 text-gray-300 hover:bg-gray-700 transition-colors"
              :disabled="compLoading"
              @click="loadComponents"
            >
              {{ compLoading ? '刷新中...' : '🔄 刷新' }}
            </button>
          </div>

          <div v-if="compLoading" class="text-sm text-gray-400 py-4">组件信息加载中...</div>
          <div v-else-if="compError" class="text-sm text-orange-400 py-4">{{ compError }}</div>
          <div v-else-if="!components.length" class="text-sm text-gray-500 py-4">
            暂无组件信息。点击右上角「同步」按钮从设备采集实体 MIB（序列号/型号/版本）。
          </div>
          <div v-else class="overflow-x-auto">
            <table class="w-full text-sm">
              <thead>
                <tr class="bg-gray-800/50 text-left text-xs text-gray-400">
                  <th class="px-3 py-2.5">索引</th>
                  <th class="px-3 py-2.5">名称</th>
                  <th class="px-3 py-2.5">型号</th>
                  <th class="px-3 py-2.5">序列号</th>
                  <th class="px-3 py-2.5">硬件版本</th>
                  <th class="px-3 py-2.5">固件版本</th>
                  <th class="px-3 py-2.5">软件版本</th>
                  <th class="px-3 py-2.5">厂商</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="c in components"
                  :key="c.phys_index"
                  class="border-t border-gray-800/60"
                  :class="c.phys_index === 2 ? 'bg-cyan-500/5' : ''"
                >
                  <td class="px-3 py-2 text-gray-500">{{ c.phys_index }}{{ c.phys_index === 2 ? '（机箱）' : '' }}</td>
                  <td class="px-3 py-2 text-gray-200">{{ c.name || '-' }}</td>
                  <td class="px-3 py-2 text-gray-200">{{ c.model_name || '-' }}</td>
                  <td class="px-3 py-2 text-cyan-300 font-mono">{{ c.serial_number || '-' }}</td>
                  <td class="px-3 py-2 text-gray-400">{{ c.hardware_rev || '-' }}</td>
                  <td class="px-3 py-2 text-gray-400">{{ c.firmware_rev || '-' }}</td>
                  <td class="px-3 py-2 text-gray-400">{{ c.software_rev || '-' }}</td>
                  <td class="px-3 py-2 text-gray-400">{{ c.mfg_name || '-' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- Performance Metrics -->
        <div class="flex items-center justify-between mt-6">
          <h2 class="text-lg font-semibold text-gray-200">实时性能指标</h2>
          <span class="text-xs px-2.5 py-1 rounded-full bg-green-900/40 text-green-400 border border-green-800">
            真实 SNMP 采集 · 每 60 秒
          </span>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
          <div class="bg-gray-900 border border-gray-800 rounded-xl p-6 flex flex-col items-center">
            <h3 class="text-sm text-gray-400 mb-4">CPU 使用率</h3>
            <svg viewBox="0 0 120 70" class="w-40 h-24">
              <path d="M 10 60 A 50 50 0 0 1 110 60" fill="none" stroke="#374151" stroke-width="10" stroke-linecap="round" />
              <path
                d="M 10 60 A 50 50 0 0 1 110 60"
                fill="none"
                :stroke="cpuColor"
                stroke-width="10"
                stroke-linecap="round"
                stroke-dasharray="157"
                :stroke-dashoffset="157 - (device.cpu_usage ?? 0) * 157 / 100"
                class="transition-all duration-700 ease-out"
              />
              <text x="60" y="48" text-anchor="middle" fill="#f3f4f6" font-size="18" font-weight="bold">
                {{ device.cpu_usage == null ? 'N/A' : device.cpu_usage + '%' }}
              </text>
              <text x="60" y="64" text-anchor="middle" fill="#9ca3af" font-size="10">CPU</text>
            </svg>
            <div v-if="device.cpu_usage == null" class="mt-2 text-xs text-gray-500 text-center leading-relaxed">
              设备 SNMP 代理未开放 CPU MIB<br/>暂无真实数据
            </div>
            <div v-else class="mt-2 flex gap-2 text-xs">
              <span class="flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-green-500" /> 正常</span>
              <span class="flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-yellow-500" /> 偏高</span>
              <span class="flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-red-500" /> 危险</span>
            </div>
          </div>

          <div class="bg-gray-900 border border-gray-800 rounded-xl p-6 flex flex-col items-center">
            <h3 class="text-sm text-gray-400 mb-4">内存使用率</h3>
            <svg viewBox="0 0 120 70" class="w-40 h-24">
              <path d="M 10 60 A 50 50 0 0 1 110 60" fill="none" stroke="#374151" stroke-width="10" stroke-linecap="round" />
              <path
                d="M 10 60 A 50 50 0 0 1 110 60"
                fill="none"
                :stroke="memColor"
                stroke-width="10"
                stroke-linecap="round"
                stroke-dasharray="157"
                :stroke-dashoffset="157 - (device.memory_usage ?? 0) * 157 / 100"
                class="transition-all duration-700 ease-out"
              />
              <text x="60" y="48" text-anchor="middle" fill="#f3f4f6" font-size="18" font-weight="bold">
                {{ device.memory_usage == null ? 'N/A' : device.memory_usage + '%' }}
              </text>
              <text x="60" y="64" text-anchor="middle" fill="#9ca3af" font-size="10">Memory</text>
            </svg>
            <div class="mt-2 flex gap-2 text-xs">
              <span class="flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-green-500" /> 正常</span>
              <span class="flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-yellow-500" /> 偏高</span>
              <span class="flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-red-500" /> 危险</span>
            </div>
          </div>

          <div class="bg-gray-900 border border-gray-800 rounded-xl p-6 flex flex-col items-center">
            <h3 class="text-sm text-gray-400 mb-4">设备温度</h3>
            <svg viewBox="0 0 120 70" class="w-40 h-24">
              <path d="M 10 60 A 50 50 0 0 1 110 60" fill="none" stroke="#374151" stroke-width="10" stroke-linecap="round" />
              <path
                d="M 10 60 A 50 50 0 0 1 110 60"
                fill="none"
                :stroke="tempColor"
                stroke-width="10"
                stroke-linecap="round"
                stroke-dasharray="157"
                :stroke-dashoffset="157 - Math.min(device.temperature ?? 0, 100) * 157 / 100"
                class="transition-all duration-700 ease-out"
              />
              <text x="60" y="48" text-anchor="middle" fill="#f3f4f6" font-size="18" font-weight="bold">
                {{ device.temperature == null ? 'N/A' : device.temperature + '°C' }}
              </text>
              <text x="60" y="64" text-anchor="middle" fill="#9ca3af" font-size="10">Temperature</text>
            </svg>
            <div v-if="device.temperature == null" class="mt-2 text-xs text-gray-500 text-center leading-relaxed">
              设备 SNMP 代理未开放温度 MIB<br/>暂无真实数据
            </div>
            <div v-else class="mt-2 flex gap-2 text-xs">
              <span class="flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-green-500" /> 正常</span>
              <span class="flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-yellow-500" /> 偏高</span>
              <span class="flex items-center gap-1"><span class="w-2 h-2 rounded-full bg-red-500" /> 过温</span>
            </div>
          </div>
        </div>

        <!-- 接口流量 TOP10（实时 SNMP） -->
        <div class="bg-gray-900 border border-gray-800 rounded-xl p-6">
          <div class="flex items-center justify-between mb-3">
            <h3 class="text-sm text-gray-400">接口流量 TOP10（实时 SNMP 采样）</h3>
            <button
              class="px-2.5 py-1 text-xs bg-gray-800 hover:bg-gray-700 rounded-lg text-gray-300 transition-colors disabled:opacity-50"
              :disabled="ifLoading"
              @click="loadInterfaces"
            >
              {{ ifLoading ? '采集中…' : '刷新' }}
            </button>
          </div>

          <div v-if="ifLoading" class="space-y-2">
            <div v-for="i in 5" :key="i" class="h-10 bg-gray-800 rounded animate-pulse" />
          </div>
          <div v-else-if="ifError" class="text-red-400 text-sm py-4">{{ ifError }}</div>
          <div v-else-if="interfaces.length === 0" class="text-gray-500 text-sm py-4">
            未采集到活跃物理接口流量（设备 SNMP 未开放接口 MIB 或端口均无流量）
          </div>
          <div v-else class="space-y-2.5">
            <div v-for="it in interfaces" :key="it.ifindex" class="flex items-center gap-3">
              <div class="w-36 shrink-0 font-mono text-xs text-gray-300 truncate" :title="it.name">{{ it.name }}</div>
              <div class="w-10 shrink-0 text-xs text-gray-500">{{ formatBw(it.speed) }}</div>
              <div class="flex-1">
                <div class="flex items-center gap-2 mb-0.5">
                  <span class="text-[10px] text-cyan-400 w-14 shrink-0">↓ 下行</span>
                  <div class="flex-1 h-1.5 bg-gray-800 rounded-full overflow-hidden">
                    <div class="h-full rounded-full bg-cyan-500 transition-all" :style="{ width: Math.min(it.in_util, 100) + '%' }" />
                  </div>
                  <span class="text-[10px] text-gray-400 w-24 shrink-0 text-right">
                    {{ it.in_util.toFixed(2) }}% ({{ formatRate(it.in_rate) }}bps)
                  </span>
                </div>
                <div class="flex items-center gap-2">
                  <span class="text-[10px] text-green-400 w-14 shrink-0">↑ 上行</span>
                  <div class="flex-1 h-1.5 bg-gray-800 rounded-full overflow-hidden">
                    <div class="h-full rounded-full bg-green-500 transition-all" :style="{ width: Math.min(it.out_util, 100) + '%' }" />
                  </div>
                  <span class="text-[10px] text-gray-400 w-24 shrink-0 text-right">
                    {{ it.out_util.toFixed(2) }}% ({{ formatRate(it.out_rate) }}bps)
                  </span>
                </div>
              </div>
            </div>
            <div class="text-[11px] text-gray-600 pt-1">带宽利用率 = 接口实时速率 ÷ 端口带宽（按上下行较大值排序）</div>
          </div>
        </div>

        <!-- 内存历史趋势（真实 SNMP 采集） -->
        <div class="bg-gray-900 border border-gray-800 rounded-xl p-6">
          <div class="flex items-center justify-between mb-3">
            <h3 class="text-sm text-gray-400">内存使用率趋势（近 24 小时 · 真实 SNMP 数据）</h3>
            <span class="text-xs text-gray-500">{{ metricsNote }}</span>
          </div>
          <div ref="memChartRef" class="w-full h-52" />
        </div>

        <!-- Lifecycle Timeline -->
        <div v-if="lifecycleStages.length > 0" class="bg-gray-900 border border-gray-800 rounded-xl p-6">
          <h2 class="text-lg font-semibold mb-6 text-gray-200">设备生命周期</h2>
          <div class="relative pt-2 pb-8">
            <!-- Track -->
            <div class="absolute top-0 left-0 right-0 h-1.5 bg-gray-800 rounded-full" />

            <!-- Past section -->
            <div
              v-if="lifecycleStages.some(s => s.isPast)"
              class="absolute top-0 left-0 h-1.5 bg-gray-600 rounded-full"
              :style="{ width: `${Math.max(...lifecycleStages.filter(s => s.isPast).map(s => s.position))}%` }"
            />

            <!-- Markers -->
            <div
              v-for="stage in lifecycleStages"
              :key="stage.label"
              class="absolute flex flex-col items-center"
              :style="{ left: `${stage.position}%`, transform: 'translateX(-50%)' }"
            >
              <div
                class="w-3 h-3 rounded-full -mt-[3px] border-2 border-gray-900"
                :class="[stage.isNow ? 'bg-gray-400 ring-2 ring-gray-400/30' : stage.color]"
              />
              <span
                class="mt-3 text-xs whitespace-nowrap"
                :class="stage.isNow ? 'text-gray-400' : stage.isPast ? 'text-gray-500' : 'text-gray-300'"
              >
                {{ stage.label }}
              </span>
              <span class="text-xs text-gray-500 whitespace-nowrap mt-0.5">
                {{ formatDate(stage.key ? device[stage.key] : null) }}
              </span>
            </div>
          </div>
        </div>

        <!-- No lifecycle data -->
        <div
          v-else
          class="bg-gray-900 border border-gray-800 rounded-xl p-6 text-center text-gray-500 text-sm"
        >
          暂无生命周期数据
        </div>

        <!-- Recent Alerts -->
        <div class="bg-gray-900 border border-gray-800 rounded-xl p-6">
          <h2 class="text-lg font-semibold mb-4 text-gray-200">近期告警</h2>

          <div v-if="alerts.length === 0" class="text-center py-12 text-gray-500 text-sm">
            <svg class="w-12 h-12 mx-auto mb-3 text-gray-700" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
                    d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            该设备暂无告警记录
          </div>

          <div v-else class="overflow-x-auto">
            <table class="w-full text-sm">
              <thead>
                <tr class="border-b border-gray-800">
                  <th class="text-left py-3 px-3 text-gray-500 font-medium text-xs uppercase">严重级别</th>
                  <th class="text-left py-3 px-3 text-gray-500 font-medium text-xs uppercase">规则名称</th>
                  <th class="text-left py-3 px-3 text-gray-500 font-medium text-xs uppercase">告警信息</th>
                  <th class="text-left py-3 px-3 text-gray-500 font-medium text-xs uppercase">触发时间</th>
                  <th class="text-left py-3 px-3 text-gray-500 font-medium text-xs uppercase">状态</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="alert in alerts"
                  :key="alert.id"
                  class="border-b border-gray-800/50 hover:bg-gray-800/30 transition-colors"
                >
                  <td class="py-3 px-3">
                    <span
                      class="inline-block px-2 py-0.5 rounded text-xs font-medium text-white"
                      :class="severityColor(alert.severity)"
                    >
                      {{ severityLabel(alert.severity) }}
                    </span>
                  </td>
                  <td class="py-3 px-3 text-gray-300">{{ alert.rule_name || '-' }}</td>
                  <td class="py-3 px-3 text-gray-400 max-w-xs truncate" :title="alert.message">
                    {{ alert.message || '-' }}
                  </td>
                  <td class="py-3 px-3 text-gray-400 whitespace-nowrap">
                    {{ formatDateTime(alert.triggered_at) }}
                  </td>
                  <td class="py-3 px-3">
                    <span
                      class="inline-block px-2 py-0.5 rounded text-xs font-medium text-white"
                      :class="alertStatusColor(alert.status)"
                    >
                      {{ alert.status === 'active' ? '活跃' : '已解决' }}
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
