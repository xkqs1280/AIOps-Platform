<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { getAlerts, deleteAlert, clearAlerts, getMe } from '../api/index.js'
import { getConfig, setConfig, unlock, test } from '../utils/voiceAlert'

const alerts = ref([])
const total = ref(0)
const loading = ref(true)
const error = ref(null)

// 语音告警设置
const voiceCfg = ref(getConfig())
const voiceTestMsg = ref('')

function toggleVoice() {
  voiceCfg.value.enabled = !voiceCfg.value.enabled
  setConfig({ enabled: voiceCfg.value.enabled })
  if (voiceCfg.value.enabled) unlock() // 用户手势中解锁
}
function setVoiceLevel() { setConfig({ level: voiceCfg.value.level }) }
function setVoiceMode() { setConfig({ mode: voiceCfg.value.mode }) }
function setVoiceVolume() { setConfig({ volume: voiceCfg.value.volume }) }
function setVoiceMute() { setConfig({ muteStart: voiceCfg.value.muteStart, muteEnd: voiceCfg.value.muteEnd }) }
function handleVoiceTest() {
  unlock()
  const info = test()
  voiceCfg.value = getConfig()
  const parts = []
  if (!info.audioSupport) parts.push('当前浏览器不支持 Web Audio，无法播放提示音')
  if (voiceCfg.value.mode === 'tts' || voiceCfg.value.mode === 'both') {
    if (info.hasSpeech && info.localChineseVoice) parts.push('本地中文语音可用，可播报内容')
    else if (info.hasSpeech) parts.push('有语音引擎但无本地中文语音（离线可能无声）')
    else parts.push('浏览器不支持语音合成')
  }
  voiceTestMsg.value = parts.length ? '已试听。' + parts.join('；') : '已试听（提示音 + 语音）。'
}

// 一键清空告警
const clearing = ref(false)
const showClearConfirm = ref(false)

function confirmClearAll() {
  if (total.value === 0) return
  showClearConfirm.value = true
}

async function handleClearAll() {
  clearing.value = true
  try {
    const res = await clearAlerts()
    const deleted = res?.deleted ?? 0
    showClearConfirm.value = false
    await fetchAlerts()
    if (deleted > 0) alert(`已清空 ${deleted} 条告警记录`)
  } catch (err) {
    const detail = err.response?.data?.detail || err.message
    alert(`清空告警失败：${detail}`)
  } finally {
    clearing.value = false
  }
}

const severityFilter = ref('')
const statusFilter = ref('')
const currentPage = ref(1)
const pageSize = 20

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

function formatDateTime(dateStr) {
  if (!dateStr) return '-'
  return new Date(dateStr).toLocaleString('zh-CN')
}

function severityColor(severity) {
  const map = {
    critical: 'bg-red-600',
    major: 'bg-orange-500',
    minor: 'bg-yellow-500',
    warning: 'bg-blue-500'
  }
  return map[severity?.toLowerCase()] || 'bg-slate-500'
}

function severityTextColor(severity) {
  const map = {
    critical: 'text-red-400',
    major: 'text-orange-400',
    minor: 'text-yellow-400',
    warning: 'text-blue-400'
  }
  return map[severity?.toLowerCase()] || 'text-slate-400'
}

function severityLabel(severity) {
  const map = {
    critical: '严重',
    major: '重要',
    minor: '次要',
    warning: '警告'
  }
  return map[severity?.toLowerCase()] || severity
}

function statusBadgeColor(status) {
  return status === 'active' ? 'bg-red-500' : 'bg-green-600'
}

function statusText(status) {
  return status === 'active' ? '活跃' : '已解决'
}

let fetchSeq = 0  // 请求序号：丢弃过期响应，防止慢请求覆盖新数据（轮询竞态）

async function fetchAlerts(silent = false) {
  const seq = ++fetchSeq
  if (!silent) loading.value = true
  error.value = null
  try {
    const params = {
      page: currentPage.value,
      page_size: pageSize
    }
    if (severityFilter.value) params.severity = severityFilter.value
    if (statusFilter.value) params.status = statusFilter.value

    const data = await getAlerts(params)
    if (seq !== fetchSeq) return // 已有更新的请求，丢弃本次过期结果
    alerts.value = data.items || []
    total.value = data.total || 0
  } catch (err) {
    if (seq !== fetchSeq) return
    error.value = err.message || '加载告警数据失败'
    alerts.value = []
    total.value = 0
  } finally {
    if (!silent && seq === fetchSeq) loading.value = false
  }
}

function goToPage(page) {
  if (page < 1 || page > totalPages.value) return
  currentPage.value = page
}

function resetFilters() {
  severityFilter.value = ''
  statusFilter.value = ''
  currentPage.value = 1
}

watch([severityFilter, statusFilter], () => {
  currentPage.value = 1
  fetchAlerts()
})

watch(currentPage, () => {
  fetchAlerts()
})

async function removeAlert(alert) {
  if (!confirm(`确定删除该告警吗？\n严重级别：${severityLabel(alert.severity)}  设备：${alert.device_name || '-'}\n此操作不可恢复。`)) {
    return
  }
  try {
    await deleteAlert(alert.id)
    await fetchAlerts()
  } catch (err) {
    alert('删除失败：' + (err.response?.data?.detail || err.message || '未知错误'))
  }
}

const visiblePages = computed(() => {
  const pages = []
  const total = totalPages.value
  const current = currentPage.value

  if (total <= 7) {
    for (let i = 1; i <= total; i++) pages.push(i)
  } else {
    pages.push(1)
    if (current > 3) pages.push('...')
    const start = Math.max(2, current - 1)
    const end = Math.min(total - 1, current + 1)
    for (let i = start; i <= end; i++) pages.push(i)
    if (current < total - 2) pages.push('...')
    pages.push(total)
  }
  return pages
})

let refreshTimer = null
onMounted(() => {
  fetchAlerts()
  // 告警列表 10s 自动刷新（静默，不闪烁 loading）
  refreshTimer = setInterval(() => fetchAlerts(true), 10000)
})
onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<template>
  <div class="min-h-screen bg-slate-950 text-slate-100 animate-in">
    <!-- Header -->
    <div class="sticky top-0 z-10 bg-slate-950/95 backdrop-blur border-b border-slate-800 px-6 py-4">
      <div class="max-w-7xl mx-auto">
        <h1 class="text-xl font-bold">告警管理</h1>
      </div>
    </div>

    <div class="max-w-7xl mx-auto p-6 space-y-6">
      <!-- Filter Bar -->
      <div class="bg-slate-900 border border-slate-800 rounded-xl p-4">
        <div class="flex flex-wrap items-center gap-4">
          <div class="flex items-center gap-2">
            <label class="text-xs text-slate-500 font-medium">严重级别</label>
            <select
              v-model="severityFilter"
              class="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200
                     focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors"
            >
              <option value="">全部</option>
              <option value="critical">严重</option>
              <option value="major">重要</option>
              <option value="minor">次要</option>
              <option value="warning">警告</option>
            </select>
          </div>

          <div class="flex items-center gap-2">
            <label class="text-xs text-slate-500 font-medium">状态</label>
            <select
              v-model="statusFilter"
              class="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200
                     focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors"
            >
              <option value="">全部</option>
              <option value="active">活跃</option>
              <option value="resolved">已解决</option>
            </select>
          </div>

          <button
            @click="resetFilters"
            class="px-3 py-2 text-sm text-slate-400 hover:text-slate-200 bg-slate-800 hover:bg-slate-700
                   rounded-lg border border-slate-700 transition-colors"
          >
            重置筛选
          </button>

          <button
            @click="confirmClearAll"
            class="px-3 py-2 text-sm text-red-300 hover:text-red-200 bg-red-900/40 hover:bg-red-800/60
                   rounded-lg border border-red-800/60 transition-colors"
            :disabled="clearing || total === 0"
          >
            {{ clearing ? '清空中...' : '一键清空' }}
          </button>

          <div class="ml-auto text-sm text-slate-500">
            共 <span class="text-slate-300 font-medium">{{ total }}</span> 条告警
          </div>
        </div>
      </div>

      <!-- 语音告警设置（音效为主 + 可选语音播报） -->
      <div class="bg-slate-900 border border-slate-800 rounded-xl p-4">
        <div class="flex flex-wrap items-center gap-x-5 gap-y-3">
          <div class="flex items-center gap-3">
            <span class="text-xs text-slate-500 font-medium">🔊 语音告警</span>
            <button
              @click="toggleVoice"
              class="relative w-11 h-6 rounded-full transition-colors focus:outline-none"
              :class="voiceCfg.enabled ? 'bg-cyan-600' : 'bg-slate-700'"
            >
              <span
                class="absolute top-0.5 w-5 h-5 rounded-full bg-white transition-all"
                :class="voiceCfg.enabled ? 'left-[22px]' : 'left-0.5'"
              />
            </button>
            <span class="text-sm" :class="voiceCfg.enabled ? 'text-cyan-400' : 'text-slate-500'">
              {{ voiceCfg.enabled ? '已开启' : '已关闭' }}
            </span>
          </div>

          <div class="flex items-center gap-2">
            <label class="text-xs text-slate-500 font-medium">播报级别</label>
            <select
              v-model="voiceCfg.level"
              @change="setVoiceLevel"
              class="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200
                     focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 transition-colors"
            >
              <option value="critical">仅严重</option>
              <option value="major">重要及以上</option>
              <option value="minor">全部</option>
            </select>
          </div>

          <div class="flex items-center gap-2">
            <label class="text-xs text-slate-500 font-medium">播报方式</label>
            <select
              v-model="voiceCfg.mode"
              @change="setVoiceMode"
              class="bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200
                     focus:outline-none focus:border-cyan-500 focus:ring-1 focus:ring-cyan-500 transition-colors"
            >
              <option value="sound">提示音（推荐，离线可靠）</option>
              <option value="tts">语音播报内容</option>
              <option value="both">提示音 + 语音</option>
            </select>
          </div>

          <div class="flex items-center gap-2">
            <label class="text-xs text-slate-500 font-medium">音量</label>
            <input
              v-model.number="voiceCfg.volume"
              type="range" min="0" max="1" step="0.05"
              @change="setVoiceVolume"
              class="w-24 accent-cyan-500"
            />
            <span class="text-xs text-slate-400 w-8">{{ Math.round((voiceCfg.volume || 0) * 100) }}%</span>
          </div>

          <div class="flex items-center gap-2">
            <label class="text-xs text-slate-500 font-medium">静音时段</label>
            <select v-model.number="voiceCfg.muteStart" @change="setVoiceMute" class="bg-slate-800 border border-slate-700 rounded-lg px-2 py-2 text-sm text-slate-200">
              <option :value="0">0时</option>
              <option v-for="h in [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23]" :key="h" :value="h">{{ h }}时</option>
            </select>
            <span class="text-slate-500 text-xs">至</span>
            <select v-model.number="voiceCfg.muteEnd" @change="setVoiceMute" class="bg-slate-800 border border-slate-700 rounded-lg px-2 py-2 text-sm text-slate-200">
              <option :value="0">0时</option>
              <option v-for="h in [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23]" :key="h" :value="h">{{ h }}时</option>
            </select>
            <span v-if="voiceCfg.muteStart === voiceCfg.muteEnd" class="text-xs text-slate-600">（相同则不静音）</span>
          </div>

          <button
            @click="handleVoiceTest"
            class="px-3 py-2 text-sm text-cyan-300 hover:text-cyan-200 bg-cyan-900/30 hover:bg-cyan-800/40
                   rounded-lg border border-cyan-800/60 transition-colors"
          >
            试听
          </button>

          <span v-if="voiceTestMsg" class="text-xs text-slate-500 max-w-md truncate" :title="voiceTestMsg">
            {{ voiceTestMsg }}
          </span>
        </div>
      </div>

      <div
        v-if="showClearConfirm"
        class="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      >
        <div class="bg-slate-900 border border-slate-700 rounded-xl p-6 w-96 shadow-2xl">
          <h3 class="text-lg font-semibold text-slate-100 mb-2">确认清空</h3>
          <p class="text-slate-400 mb-2">
            确定要清空 <span class="text-red-400 font-semibold">{{ total }}</span> 条告警记录吗？
          </p>
          <p class="text-red-500/90 text-sm mb-6">此操作不可撤销，请确认。</p>
          <div class="flex justify-end gap-3">
            <button
              class="px-4 py-2 bg-slate-700 hover:bg-slate-600 rounded-lg text-slate-300 transition-colors"
              @click="showClearConfirm = false"
            >
              取消
            </button>
            <button
              class="btn btn-danger"
              :disabled="clearing"
              @click="handleClearAll"
            >
              {{ clearing ? '清空中...' : '确认清空' }}
            </button>
          </div>
        </div>
      </div>

      <!-- Loading -->
      <div v-if="loading" class="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden">
        <div class="animate-pulse">
          <div v-for="i in 8" :key="i" class="flex items-center gap-4 px-6 py-4 border-b border-slate-800/50">
            <div class="h-5 w-16 bg-slate-800 rounded" />
            <div class="h-5 w-24 bg-slate-800 rounded" />
            <div class="h-5 w-32 bg-slate-800 rounded" />
            <div class="h-5 flex-1 bg-slate-800 rounded" />
            <div class="h-5 w-28 bg-slate-800 rounded" />
            <div class="h-5 w-12 bg-slate-800 rounded" />
          </div>
        </div>
      </div>

      <!-- Error -->
      <div v-else-if="error" class="bg-slate-900 border border-slate-800 rounded-xl p-12 text-center">
        <div class="text-red-400 text-lg mb-4">{{ error }}</div>
        <button
          @click="fetchAlerts"
          class="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded-lg border border-slate-700 transition-colors"
        >
          重试
        </button>
      </div>

      <!-- Empty -->
      <div
        v-else-if="alerts.length === 0"
        class="bg-slate-900 border border-slate-800 rounded-xl p-16 text-center"
      >
        <svg class="w-16 h-16 mx-auto mb-4 text-slate-700" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
                d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        <p class="text-slate-500 text-lg mb-1">暂无告警记录</p>
        <p class="text-slate-600 text-sm">
          {{ severityFilter || statusFilter ? '当前筛选条件下没有匹配的告警，尝试调整筛选条件' : '系统运行正常，没有告警产生' }}
        </p>
        <button
          v-if="severityFilter || statusFilter"
          @click="resetFilters"
          class="mt-4 px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg transition-colors text-sm"
        >
          清除筛选条件
        </button>
      </div>

      <!-- Alert Table -->
      <div v-else class="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden">
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="border-b border-slate-800 bg-slate-900">
                <th class="text-left py-3.5 px-4 text-slate-500 font-medium text-xs uppercase tracking-wider">
                  严重级别
                </th>
                <th class="text-left py-3.5 px-4 text-slate-500 font-medium text-xs uppercase tracking-wider">
                  设备名称
                </th>
                <th class="text-left py-3.5 px-4 text-slate-500 font-medium text-xs uppercase tracking-wider">
                  规则名称
                </th>
                <th class="text-left py-3.5 px-4 text-slate-500 font-medium text-xs uppercase tracking-wider">
                  告警信息
                </th>
                <th class="text-left py-3.5 px-4 text-slate-500 font-medium text-xs uppercase tracking-wider">
                  触发时间
                </th>
                <th class="text-left py-3.5 px-4 text-slate-500 font-medium text-xs uppercase tracking-wider">
                  解决时间
                </th>
                <th class="text-left py-3.5 px-4 text-slate-500 font-medium text-xs uppercase tracking-wider">
                  状态
                </th>
                <th class="text-left py-3.5 px-4 text-slate-500 font-medium text-xs uppercase tracking-wider">
                  操作
                </th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="alert in alerts"
                :key="alert.id"
                class="border-b border-slate-800/50 hover:bg-slate-800/30 transition-colors"
              >
                <td class="py-3 px-4">
                  <span
                    class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-semibold text-white"
                    :class="severityColor(alert.severity)"
                  >
                    {{ severityLabel(alert.severity) }}
                  </span>
                </td>
                <td class="py-3 px-4 text-slate-200 font-medium">{{ alert.device_name || '-' }}</td>
                <td class="py-3 px-4 text-slate-300">{{ alert.rule_name || '-' }}</td>
                <td class="py-3 px-4 text-slate-400 max-w-sm">
                  <span class="truncate block" :title="alert.message">
                    {{ alert.message || '-' }}
                  </span>
                </td>
                <td class="py-3 px-4 text-slate-400 whitespace-nowrap">
                  {{ formatDateTime(alert.triggered_at) }}
                </td>
                <td class="py-3 px-4 text-slate-400 whitespace-nowrap">
                  {{ formatDateTime(alert.resolved_at) }}
                </td>
                <td class="py-3 px-4">
                  <span
                    class="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-semibold text-white"
                    :class="statusBadgeColor(alert.status)"
                  >
                    <span
                      class="w-1.5 h-1.5 rounded-full"
                      :class="alert.status === 'active' ? 'bg-red-200 animate-pulse' : 'bg-green-200'"
                    />
                    {{ statusText(alert.status) }}
                  </span>
                </td>
                <td class="py-3 px-4">
                  <button
                    @click="removeAlert(alert)"
                    class="px-2.5 py-1 text-xs rounded bg-red-500/10 text-red-400 border border-red-500/20 hover:bg-red-500/20 transition-colors"
                  >
                    删除
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Pagination -->
      <div
        v-if="alerts.length > 0 && totalPages > 1"
        class="flex items-center justify-between bg-slate-900 border border-slate-800 rounded-xl px-6 py-4"
      >
        <div class="text-sm text-slate-500">
          第 {{ (currentPage - 1) * pageSize + 1 }}-{{ Math.min(currentPage * pageSize, total) }} 条，共 {{ total }} 条
        </div>

        <div class="flex items-center gap-1.5">
          <button
            @click="goToPage(currentPage - 1)"
            :disabled="currentPage === 1"
            class="px-3 py-1.5 text-sm rounded-lg transition-colors
                   disabled:opacity-40 disabled:cursor-not-allowed
                   text-slate-400 hover:text-slate-200 hover:bg-slate-800"
          >
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7" />
            </svg>
          </button>

          <template v-for="page in visiblePages" :key="page">
            <span v-if="page === '...'" class="px-2 text-slate-600 text-sm">...</span>
            <button
              v-else
              @click="goToPage(page)"
              class="w-8 h-8 text-sm rounded-lg transition-colors font-medium"
              :class="page === currentPage
                ? 'bg-blue-600 text-white'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'"
            >
              {{ page }}
            </button>
          </template>

          <button
            @click="goToPage(currentPage + 1)"
            :disabled="currentPage === totalPages"
            class="px-3 py-1.5 text-sm rounded-lg transition-colors
                   disabled:opacity-40 disabled:cursor-not-allowed
                   text-slate-400 hover:text-slate-200 hover:bg-slate-800"
          >
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
            </svg>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
