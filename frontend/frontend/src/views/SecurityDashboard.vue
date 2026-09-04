<template>
  <div class="min-h-screen bg-app text-ink-strong p-4 flex flex-col gap-4 animate-in">
    <!-- Header -->
    <div class="flex items-center justify-between px-2">
      <div class="flex items-center gap-3">
        <div class="w-2 h-8 bg-gradient-to-b from-red-400 to-pink-600 rounded-full"></div>
        <h1 class="text-2xl font-bold tracking-wide">安全监控</h1>
      </div>
      <div class="flex items-center gap-2">
        <span class="text-xs text-ink-faint">自动刷新: 15s</span>
        <button
          @click="aiOpen = true"
          class="px-3 py-1.5 text-xs rounded-lg bg-violet-500/10 text-violet-400 border border-violet-500/20 hover:bg-violet-500/20 transition-colors"
        >AI 运维日报</button>
      </div>
    </div>

    <!-- 外部实时威胁态势（FireHOL 开放情报 + ipwho.is） -->
    <div class="bg-surface rounded-xl border border-line p-4 flex flex-col gap-4">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2 border-l-4 border-red-500 pl-2">
          <span class="text-sm font-semibold text-ink">外部实时威胁态势</span>
          <span class="text-xs text-red-400/80">全球恶意攻击源监测</span>
        </div>
        <span class="text-xs text-ink-faint text-right">{{ threatNote }}</span>
      </div>

      <!-- Stat Cards -->
      <div class="grid grid-cols-4 gap-3">
        <div class="bg-surface-2/50 rounded-lg p-3">
          <span class="text-xs text-ink-muted">恶意源条目总数</span>
          <span class="block text-3xl font-bold text-red-400 mt-1">{{ latestThreat.total_entries || 0 }}</span>
          <span class="text-xs text-ink-faint">实时恶意 IP/CIDR 条目</span>
        </div>
        <div class="bg-surface-2/50 rounded-lg p-3">
          <span class="text-xs text-ink-muted">涉及中国来源(抽样)</span>
          <span class="block text-3xl font-bold text-yellow-400 mt-1">{{ latestThreat.china_entries || 0 }}</span>
          <span class="text-xs text-ink-faint">抽样 {{ latestThreat.sampled_ips || 0 }} 个 IP</span>
        </div>
        <div class="bg-surface-2/50 rounded-lg p-3">
          <span class="text-xs text-ink-muted">攻击类型</span>
          <span class="block text-3xl font-bold text-cyan-400 mt-1">{{ threatTypeCount }}</span>
          <span class="text-xs text-ink-faint">{{ threatTypeSummary }}</span>
        </div>
        <div class="bg-surface-2/50 rounded-lg p-3">
          <span class="text-xs text-ink-muted">最近更新</span>
          <span class="block text-2xl font-bold text-ink-strong mt-1 text-lg leading-9">{{ threatUpdatedAt }}</span>
          <span class="text-xs text-ink-faint">每 30 分钟自动刷新</span>
        </div>
      </div>

      <!-- Charts -->
      <div class="grid grid-cols-2 gap-3">
        <div>
          <div class="text-xs text-ink-faint mb-1">攻击类型分布（恶意源条目数）</div>
          <div ref="extTypeChartRef" class="w-full h-52"></div>
        </div>
        <div>
          <div class="text-xs text-ink-faint mb-1">恶意源来源国家 TOP10（抽样，中国黄色高亮）</div>
          <div ref="extCountryChartRef" class="w-full h-52"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- AI 运维日报面板 -->
  <AiPanel v-model:open="aiOpen" title="AI 运维日报" path="/ai/report/daily" />
</template>

<script setup>
import { chartTheme } from '../utils/chartTheme'
const cc = chartTheme()
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import * as echarts from 'echarts'
import AiPanel from '../components/AiPanel.vue'

const aiOpen = ref(false)

const API_BASE = '/api/v1'

const latestThreat = ref({})
const threatNote = ref('')

const extTypeChartRef = ref(null)
const extCountryChartRef = ref(null)

let extTypeChart = null
let extCountryChart = null
let refreshTimer = null

const threatTypeCount = computed(() => Object.keys(latestThreat.value.type_data || {}).length)
const threatTypeSummary = computed(() => {
  const td = latestThreat.value.type_data || {}
  return Object.entries(td).map(([k, v]) => `${k} ${v}`).join(' · ') || '暂无数据'
})
const threatUpdatedAt = computed(() => {
  const t = latestThreat.value.sampled_at
  if (!t) return '—'
  const d = new Date(t)
  if (isNaN(d.getTime())) return t
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
})

async function fetchExternalThreat() {
  try {
    const res = await fetch(`${API_BASE}/security/external/latest`)
    const data = await res.json()
    latestThreat.value = data.latest || {}
    threatNote.value = data.source_note || ''
  } catch (err) {
    console.error('External threat fetch error:', err)
  }
}

function renderExtTypeChart() {
  if (!extTypeChartRef.value) return
  if (!extTypeChart) extTypeChart = echarts.init(extTypeChartRef.value)
  const td = latestThreat.value.type_data || {}
  const entries = Object.entries(td).map(([name, value]) => ({ name, value }))
  extTypeChart.setOption({
    color: ['#f87171', '#fb923c', '#facc15', '#38bdf8', '#a78bfa', '#34d399'],
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, backgroundColor: cc.tooltipBg, borderColor: cc.tooltipBorder, textStyle: { color: cc.text } },
    grid: { left: '3%', right: '12%', top: '3%', bottom: '3%', containLabel: true },
    xAxis: {
      type: 'value',
      axisLabel: { color: cc.sub, fontSize: 9 },
      splitLine: { lineStyle: { color: cc.split } },
    },
    yAxis: {
      type: 'category',
      data: entries.map(d => d.name).reverse(),
      axisLabel: { color: cc.sub, fontSize: 11 },
      axisLine: { lineStyle: { color: cc.tooltipBorder } },
    },
    series: [{
      type: 'bar',
      data: entries.map(d => d.value).reverse(),
      barWidth: '45%',
      itemStyle: { borderRadius: [0, 4, 4, 0] },
      label: { show: true, position: 'right', color: cc.sub, fontSize: 10 },
    }],
  }, true)
}

function renderExtCountryChart() {
  if (!extCountryChartRef.value) return
  if (!extCountryChart) extCountryChart = echarts.init(extCountryChartRef.value)
  const top = latestThreat.value.country_top || []
  const data = top.slice(0, 10)
  extCountryChart.setOption({
    color: ['#22d3ee'],
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, backgroundColor: cc.tooltipBg, borderColor: cc.tooltipBorder, textStyle: { color: cc.text } },
    grid: { left: '3%', right: '12%', top: '3%', bottom: '3%', containLabel: true },
    xAxis: {
      type: 'value',
      axisLabel: { color: cc.sub, fontSize: 9 },
      splitLine: { lineStyle: { color: cc.split } },
    },
    yAxis: {
      type: 'category',
      data: data.map(d => d.name).reverse(),
      axisLabel: { color: cc.sub, fontSize: 11 },
      axisLine: { lineStyle: { color: cc.tooltipBorder } },
    },
    series: [{
      type: 'bar',
      data: data.map(d => d.count).reverse(),
      barWidth: '45%',
      itemStyle: {
        borderRadius: [0, 4, 4, 0],
        color: function (params) {
          const item = data[data.length - 1 - params.dataIndex]
          return item && item.is_china ? '#f59e0b' : '#22d3ee'
        },
      },
      label: { show: true, position: 'right', color: cc.sub, fontSize: 10 },
    }],
  }, true)
}

function renderAllCharts() {
  renderExtTypeChart()
  renderExtCountryChart()
}

function handleResize() {
  extTypeChart?.resize()
  extCountryChart?.resize()
}

onMounted(async () => {
  await fetchExternalThreat()
  await nextTick()
  renderAllCharts()
  refreshTimer = setInterval(async () => {
    await fetchExternalThreat()
    await nextTick()
    renderAllCharts()
  }, 15000)
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
  window.removeEventListener('resize', handleResize)
  extTypeChart?.dispose()
  extCountryChart?.dispose()
})
</script>

<style scoped>
.custom-scrollbar::-webkit-scrollbar { width: 4px; }
.custom-scrollbar::-webkit-scrollbar-track { background: var(--line-strong); border-radius: 2px; }
.custom-scrollbar::-webkit-scrollbar-thumb { background: var(--ink-faint); border-radius: 2px; }
.custom-scrollbar::-webkit-scrollbar-thumb:hover { background: #6b7280; }
</style>
