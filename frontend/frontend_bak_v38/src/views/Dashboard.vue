<template>
  <div class="min-h-screen bg-gray-950 text-gray-100 p-4 flex flex-col gap-4">
    <!-- 授权到期预警横幅 -->
    <div
      v-if="licenseWarning"
      class="flex items-center justify-center gap-2 bg-orange-500/15 border border-orange-500/40 text-orange-300 text-sm rounded-lg px-4 py-2.5"
    >
      <span>⚠️</span>
      <span>{{ licenseWarning }}</span>
      <a href="mailto:x1280455974@163.com" class="underline hover:text-orange-200 ml-2">联系授权邮箱 x1280455974@163.com</a>
      <router-link to="/settings/license" class="underline hover:text-orange-200 ml-2">前往授权管理</router-link>
    </div>
    <!-- Header -->
    <div class="flex items-center justify-between px-2">
      <div class="flex items-center gap-3">
        <div class="w-2 h-8 bg-gradient-to-b from-cyan-400 to-blue-600 rounded-full"></div>
        <h1 class="text-2xl font-bold tracking-wide">AIOps 网络监控平台</h1>
      </div>
      <div class="flex items-center gap-4 text-sm text-gray-400">
        <span class="flex items-center gap-2">
          <span class="w-2 h-2 bg-green-400 rounded-full animate-pulse"></span>
          实时监控中
        </span>
        <span>{{ currentTime }}</span>
        <span class="text-gray-600">|</span>
        <span>刷新间隔: 10s</span>
      </div>
    </div>

    <!-- Row 1: h-48 -->
    <div class="grid grid-cols-12 gap-4 h-48">
      <!-- Device Health Overview -->
      <div class="col-span-2 bg-gray-900 rounded-xl border border-gray-800 p-4 flex flex-col">
        <PanelTitle title="设备健康概览" accent="cyan" />
        <div class="grid grid-cols-2 gap-2 flex-1 mt-3">
          <div class="bg-gray-800/50 rounded-lg p-2 flex flex-col items-center justify-center">
            <span class="text-2xl font-bold text-gray-100">{{ overview.total_devices || 0 }}</span>
            <span class="text-xs text-gray-400 mt-1">设备总数</span>
          </div>
          <div class="bg-green-900/20 rounded-lg p-2 flex flex-col items-center justify-center border border-green-800/30">
            <span class="text-2xl font-bold text-green-400">{{ overview.online || 0 }}</span>
            <span class="text-xs text-green-500/70 mt-1">在线</span>
          </div>
          <div class="bg-red-900/20 rounded-lg p-2 flex flex-col items-center justify-center border border-red-800/30">
            <span class="text-2xl font-bold text-red-400">{{ overview.offline || 0 }}</span>
            <span class="text-xs text-red-500/70 mt-1">离线</span>
          </div>
          <div class="bg-red-900/20 rounded-lg p-2 flex flex-col items-center justify-center border border-red-800/30">
            <span class="text-2xl font-bold text-red-400">{{ overview.active_alerts || 0 }}</span>
            <span class="text-xs text-red-500/70 mt-1">活跃告警</span>
          </div>
        </div>
      </div>

      <!-- Device Type Distribution -->
      <div class="col-span-3 bg-gray-900 rounded-xl border border-gray-800 p-4 flex flex-col">
        <PanelTitle title="设备类型分布" accent="blue" />
        <div ref="typeChartRef" class="flex-1 w-full"></div>
      </div>

      <!-- Vendor Distribution -->
      <div class="col-span-3 bg-gray-900 rounded-xl border border-gray-800 p-4 flex flex-col">
        <PanelTitle title="厂商分布" accent="purple" />
        <div ref="vendorChartRef" class="flex-1 w-full"></div>
      </div>

      <!-- Real-time Alert Scrolling -->
      <div class="col-span-4 bg-gray-900 rounded-xl border border-gray-800 p-4 flex flex-col">
        <PanelTitle title="实时告警滚动" accent="red" />
        <div class="flex-1 overflow-hidden relative mt-2" @mouseenter="pauseScroll" @mouseleave="resumeScroll">
          <div ref="alertScrollRef" class="absolute inset-0 overflow-hidden">
            <div
              v-for="(alert, index) in displayAlerts"
              :key="index"
              class="flex items-center gap-3 py-1.5 px-2 rounded-lg hover:bg-gray-800/50 transition-colors"
            >
              <span
                class="w-1.5 h-6 rounded-full flex-shrink-0"
                :class="severityBarClass(alert.severity)"
              ></span>
              <span
                class="text-xs px-1.5 py-0.5 rounded font-medium flex-shrink-0"
                :class="severityBadgeClass(alert.severity)"
              >{{ severityLabel(alert.severity) }}</span>
              <span class="text-sm text-gray-300 truncate flex-1">{{ alert.device_name }} - {{ alert.message }}</span>
              <span class="text-xs text-gray-500 flex-shrink-0">{{ formatTime(alert.triggered_at) }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Row 2: h-80 -->
    <div class="grid grid-cols-12 gap-4 h-80">
      <!-- CPU Usage TOP5 -->
      <div class="col-span-2 bg-gray-900 rounded-xl border border-gray-800 p-4 flex flex-col">
        <PanelTitle title="CPU 使用率 TOP5" accent="orange" />
        <div ref="cpuChartRef" class="flex-1 w-full"></div>
      </div>

      <!-- Network Topology Graph -->
      <div class="col-span-9 bg-gray-900 rounded-xl border border-gray-800 p-4 flex flex-col">
        <PanelTitle title="网络拓扑图" accent="cyan" />
        <div ref="topologyChartRef" class="flex-1 w-full"></div>
      </div>

      <!-- Memory Usage TOP5 -->
      <div class="col-span-1 bg-gray-900 rounded-xl border border-gray-800 p-4 flex flex-col">
        <PanelTitle title="内存 TOP5" accent="pink" />
        <div ref="memoryChartRef" class="flex-1 w-full"></div>
      </div>
    </div>

    <!-- Row 3: h-52 -->
    <div class="grid grid-cols-12 gap-4 h-52">
      <!-- Bandwidth Utilization TOP10 -->
      <div class="col-span-6 bg-gray-900 rounded-xl border border-gray-800 p-4 flex flex-col">
        <PanelTitle title="带宽利用率 TOP10" accent="green" />
        <div ref="bandwidthChartRef" class="flex-1 w-full"></div>
      </div>

      <!-- Device Lifecycle Reminders -->
      <div class="col-span-3 bg-gray-900 rounded-xl border border-gray-800 p-4 flex flex-col">
        <PanelTitle title="设备生命周期提醒" accent="yellow" />
        <div class="flex-1 overflow-y-auto mt-2 custom-scrollbar">
          <div
            v-for="(item, index) in lifecycleReminders"
            :key="index"
            class="flex items-center gap-2 py-1.5 px-2 rounded-lg hover:bg-gray-800/50"
          >
            <span class="w-2 h-2 rounded-full flex-shrink-0" :class="lifecycleDotClass(item.severity)"></span>
            <span class="text-sm text-gray-300 truncate flex-1">{{ item.device_name }}</span>
            <span class="text-xs px-1.5 py-0.5 rounded" :class="lifecycleBadgeClass(item.type)">{{ item.type }}</span>
            <span class="text-xs text-gray-500 flex-shrink-0">{{ item.date }}</span>
          </div>
          <div v-if="lifecycleReminders.length === 0" class="text-center text-gray-500 text-sm py-8">暂无提醒</div>
        </div>
      </div>

      <!-- 活跃告警概览（与告警管理活跃告警口径一致） -->
      <div class="col-span-3 bg-gray-900 rounded-xl border border-gray-800 p-4 flex flex-col">
        <PanelTitle title="活跃告警概览" accent="red" />
        <div class="flex-1 flex flex-col justify-center gap-3 mt-2">
          <div class="flex items-center justify-between bg-red-900/20 rounded-lg px-4 py-2 border border-red-800/30">
            <div class="flex items-center gap-3">
              <div class="w-3 h-3 bg-red-500 rounded-full animate-pulse"></div>
              <span class="text-sm text-gray-300">严重</span>
            </div>
            <span class="text-2xl font-bold text-red-400">{{ alertSummary.critical || 0 }}</span>
          </div>
          <div class="flex items-center justify-between bg-orange-900/20 rounded-lg px-4 py-2 border border-orange-800/30">
            <div class="flex items-center gap-3">
              <div class="w-3 h-3 bg-orange-500 rounded-full"></div>
              <span class="text-sm text-gray-300">主要</span>
            </div>
            <span class="text-2xl font-bold text-orange-400">{{ alertSummary.major || 0 }}</span>
          </div>
          <div class="flex items-center justify-between bg-yellow-900/20 rounded-lg px-4 py-2 border border-yellow-800/30">
            <div class="flex items-center gap-3">
              <div class="w-3 h-3 bg-yellow-500 rounded-full"></div>
              <span class="text-sm text-gray-300">次要</span>
            </div>
            <span class="text-2xl font-bold text-yellow-400">{{ alertSummary.minor || 0 }}</span>
          </div>
          <div class="flex items-center justify-between bg-blue-900/20 rounded-lg px-4 py-2 border border-blue-800/30">
            <div class="flex items-center gap-3">
              <div class="w-3 h-3 bg-blue-500 rounded-full"></div>
              <span class="text-sm text-gray-300">警告</span>
            </div>
            <span class="text-2xl font-bold text-blue-400">{{ alertSummary.warning || 0 }}</span>
          </div>
          <div class="flex items-center justify-between bg-gray-800/50 rounded-lg px-4 py-2 border border-gray-700/50">
            <div class="flex items-center gap-3">
              <div class="w-3 h-3 bg-gray-400 rounded-full"></div>
              <span class="text-sm text-gray-300">活跃告警总数</span>
            </div>
            <span class="text-2xl font-bold text-gray-100">{{ alertSummary.total || 0 }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import * as echarts from 'echarts'
import {
  getDashboardOverview,
  getCpuRanking,
  getMemoryRanking,
  getBandwidthRanking,
  getDashboardLifecycle,
  getRecentAlerts,
  getAlertStats,
  getTopology,
  getLicenseStatus
} from '../api/index.js'
import { buildTopologyNodes, buildTopologyEdges, statusColors } from '../utils/topologyConfig.js'

// --- Reactive Data ---
const overview = ref({})
const cpuRanking = ref([])
const memoryRanking = ref([])
const bandwidthRanking = ref([])
const lifecycleReminders = ref([])
const recentAlerts = ref([])
const alertStats = ref({})
const alertSummary = ref({})
const topologyData = ref({ nodes: [], edges: [] })
const currentTime = ref('')
const displayAlerts = ref([])
const licenseWarning = ref('')

// --- Chart Refs ---
const typeChartRef = ref(null)
const vendorChartRef = ref(null)
const cpuChartRef = ref(null)
const memoryChartRef = ref(null)
const bandwidthChartRef = ref(null)
const topologyChartRef = ref(null)
// 告警滚动容器
const alertScrollRef = ref(null)

// --- Chart Instances ---
let typeChart = null
let vendorChart = null
let cpuChart = null
let memoryChart = null
let bandwidthChart = null
let topologyChart = null

// --- Timers ---
let refreshTimer = null
let scrollTimer = null
let timeTimer = null
let scrollOffset = 0
let isScrollPaused = false

// --- Panel Title Component ---
import { h } from 'vue'
const PanelTitle = {
  props: ['title', 'accent'],
  setup(props) {
    const accentMap = {
      cyan: 'border-cyan-400',
      blue: 'border-blue-400',
      purple: 'border-purple-400',
      red: 'border-red-400',
      orange: 'border-orange-400',
      pink: 'border-pink-400',
      green: 'border-green-400',
      yellow: 'border-yellow-400'
    }
    return () => h('div', {
      class: `flex items-center gap-2 border-l-4 ${accentMap[props.accent] || 'border-gray-400'} pl-2 mb-2`
    }, [
      h('span', { class: 'text-sm font-semibold text-gray-200 tracking-wide' }, props.title)
    ])
  }
}

// --- Severity Helpers ---
function severityBarClass(severity) {
  const map = {
    critical: 'bg-red-500',
    major: 'bg-orange-500',
    minor: 'bg-yellow-500',
    warning: 'bg-blue-500'
  }
  return map[severity] || 'bg-gray-500'
}

function severityBadgeClass(severity) {
  const map = {
    critical: 'bg-red-500/20 text-red-400 border border-red-500/30',
    major: 'bg-orange-500/20 text-orange-400 border border-orange-500/30',
    minor: 'bg-yellow-500/20 text-yellow-400 border border-yellow-500/30',
    warning: 'bg-blue-500/20 text-blue-400 border border-blue-500/30'
  }
  return map[severity] || 'bg-gray-500/20 text-gray-400 border border-gray-500/30'
}

function severityLabel(severity) {
  const map = {
    critical: '严重',
    major: '主要',
    minor: '次要',
    warning: '警告'
  }
  return map[severity] || severity
}

function lifecycleDotClass(severity) {
  const map = {
    critical: 'bg-red-500',
    major: 'bg-orange-500',
    minor: 'bg-yellow-500',
    warning: 'bg-blue-500'
  }
  return map[severity] || 'bg-gray-500'
}

function lifecycleBadgeClass(type) {
  const map = {
    '过保': 'bg-red-500/20 text-red-400 border border-red-500/30',
    '维保到期': 'bg-orange-500/20 text-orange-400 border border-orange-500/30',
    '寿命到期': 'bg-yellow-500/20 text-yellow-400 border border-yellow-500/30',
    '保修': 'bg-blue-500/20 text-blue-400 border border-blue-500/30'
  }
  return map[type] || 'bg-gray-500/20 text-gray-400 border border-gray-500/30'
}

function formatTime(timeStr) {
  if (!timeStr) return ''
  const d = new Date(timeStr)
  if (isNaN(d.getTime())) return timeStr
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`
}

// --- Data Fetching ---
async function fetchAllData() {
  try {
    const [overviewRes, cpuRes, memRes, lifecycleRes, alertRes, topoRes] = await Promise.all([
      getDashboardOverview(),
      getCpuRanking(),
      getMemoryRanking(),
      getDashboardLifecycle(),
      getRecentAlerts(),
      getTopology()
    ])

    overview.value = overviewRes.data || {}
    cpuRanking.value = cpuRes.data || []
    memoryRanking.value = memRes.data || []
    lifecycleReminders.value = lifecycleRes.data || []
    recentAlerts.value = alertRes.data || []
    alertSummary.value = alertRes.summary || {}
    topologyData.value = topoRes.data || { nodes: [], edges: [] }

    // Duplicate alerts for seamless scrolling
    if (recentAlerts.value.length > 0) {
      displayAlerts.value = [...recentAlerts.value, ...recentAlerts.value]
    } else {
      displayAlerts.value = []
    }
    scrollOffset = 0

    // Re-render charts
    await nextTick()
    renderAllCharts()
  } catch (err) {
    console.error('Dashboard data fetch error:', err)
  }
}

// 带宽利用率 TOP10：真实 SNMP 采集较慢（约 10-25s），独立低频刷新，
// 避免阻塞大屏每 10 秒的主数据刷新。
let bandwidthTimer = null
async function fetchBandwidth() {
  try {
    const bwRes = await getBandwidthRanking()
    bandwidthRanking.value = bwRes.data || []
    await nextTick()
    renderBandwidthChart()
  } catch (err) {
    console.error('Bandwidth ranking fetch error:', err)
  }
}

// --- Chart Renderers ---
function renderTypeChart() {
  if (!typeChartRef.value) return
  if (!typeChart) {
    typeChart = echarts.init(typeChartRef.value)
  }
  const raw = overview.value.type_distribution || {}
  const data = typeof raw === 'object' && !Array.isArray(raw)
    ? Object.entries(raw).map(([name, value]) => ({ name, value }))
    : (Array.isArray(raw) ? raw : [])
  typeChart.setOption({
    tooltip: { trigger: 'item', backgroundColor: 'rgba(17,24,39,0.9)', borderColor: '#374151', textStyle: { color: '#e5e7eb' } },
    legend: { type: 'scroll', bottom: 0, textStyle: { color: '#9ca3af', fontSize: 10 }, itemWidth: 8, itemHeight: 8 },
    series: [{
      type: 'pie',
      radius: ['35%', '65%'],
      center: ['50%', '45%'],
      label: { color: '#9ca3af', fontSize: 10 },
      labelLine: { lineStyle: { color: '#4b5563' } },
      itemStyle: { borderColor: '#111827', borderWidth: 2 },
      data: data
    }],
    color: ['#06b6d4', '#3b82f6', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981', '#ef4444', '#6366f1']
  })
}

function renderVendorChart() {
  if (!vendorChartRef.value) return
  if (!vendorChart) {
    vendorChart = echarts.init(vendorChartRef.value)
  }
  const raw = overview.value.vendor_distribution || {}
  const data = typeof raw === 'object' && !Array.isArray(raw)
    ? Object.entries(raw).map(([name, value]) => ({ name, value }))
    : (Array.isArray(raw) ? raw : [])
  vendorChart.setOption({
    tooltip: { trigger: 'item', backgroundColor: 'rgba(17,24,39,0.9)', borderColor: '#374151', textStyle: { color: '#e5e7eb' } },
    legend: { type: 'scroll', bottom: 0, textStyle: { color: '#9ca3af', fontSize: 10 }, itemWidth: 8, itemHeight: 8 },
    series: [{
      type: 'pie',
      radius: ['35%', '65%'],
      center: ['50%', '45%'],
      label: { color: '#9ca3af', fontSize: 10 },
      labelLine: { lineStyle: { color: '#4b5563' } },
      itemStyle: { borderColor: '#111827', borderWidth: 2 },
      data: data
    }],
    color: ['#8b5cf6', '#06b6d4', '#f59e0b', '#10b981', '#ef4444', '#3b82f6', '#ec4899', '#6366f1']
  })
}

function renderCpuChart() {
  if (!cpuChartRef.value) return
  if (!cpuChart) {
    cpuChart = echarts.init(cpuChartRef.value)
  }
  const data = cpuRanking.value.slice(0, 5)
  cpuChart.setOption({
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: 'rgba(17,24,39,0.9)',
      borderColor: '#374151',
      textStyle: { color: '#e5e7eb' }
    },
    grid: { left: '3%', right: '15%', top: '5%', bottom: '3%', containLabel: true },
    xAxis: {
      type: 'value',
      max: 100,
      axisLabel: { color: '#6b7280', fontSize: 9, formatter: '{value}%' },
      splitLine: { lineStyle: { color: '#1f2937' } }
    },
    yAxis: {
      type: 'category',
      data: data.map(d => d.name).reverse(),
      axisLabel: { color: '#9ca3af', fontSize: 10, width: 60, overflow: 'truncate' },
      axisLine: { lineStyle: { color: '#374151' } }
    },
    series: [{
      type: 'bar',
      data: data.map(d => d.cpu_usage).reverse(),
      barWidth: '55%',
      itemStyle: {
        borderRadius: [0, 4, 4, 0],
        color: function (params) {
          const val = params.value
          if (val >= 90) return '#ef4444'
          if (val >= 70) return '#f59e0b'
          if (val >= 50) return '#eab308'
          return '#10b981'
        }
      },
      label: {
        show: true,
        position: 'right',
        color: '#9ca3af',
        fontSize: 10,
        formatter: '{c}%'
      }
    }]
  })
}

function renderMemoryChart() {
  if (!memoryChartRef.value) return
  if (!memoryChart) {
    memoryChart = echarts.init(memoryChartRef.value)
  }
  const data = memoryRanking.value.slice(0, 5)
  memoryChart.setOption({
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: 'rgba(17,24,39,0.9)',
      borderColor: '#374151',
      textStyle: { color: '#e5e7eb' }
    },
    grid: { left: '3%', right: '25%', top: '5%', bottom: '3%', containLabel: false },
    xAxis: { type: 'value', max: 100, show: false },
    yAxis: {
      type: 'category',
      data: data.map(d => d.name).reverse(),
      axisLabel: { color: '#9ca3af', fontSize: 8, width: 50, overflow: 'truncate' },
      axisLine: { lineStyle: { color: '#374151' } },
      axisTick: { show: false }
    },
    series: [{
      type: 'bar',
      data: data.map(d => d.memory_usage).reverse(),
      barWidth: '50%',
      itemStyle: {
        borderRadius: [0, 3, 3, 0],
        color: function (params) {
          const val = params.value
          if (val >= 90) return '#ef4444'
          if (val >= 70) return '#f59e0b'
          if (val >= 50) return '#eab308'
          return '#ec4899'
        }
      },
      label: {
        show: true,
        position: 'right',
        color: '#9ca3af',
        fontSize: 8,
        formatter: '{c}%'
      }
    }]
  })
}

function renderBandwidthChart() {
  if (!bandwidthChartRef.value) return
  if (!bandwidthChart) {
    bandwidthChart = echarts.init(bandwidthChartRef.value)
  }
  const data = bandwidthRanking.value.slice(0, 10)
  const fmtRate = (bps) => {
    if (!bps) return '0bps'
    if (bps >= 1e9) return (bps / 1e9).toFixed(2) + 'Gbps'
    if (bps >= 1e6) return (bps / 1e6).toFixed(2) + 'Mbps'
    if (bps >= 1e3) return (bps / 1e3).toFixed(2) + 'Kbps'
    return bps + 'bps'
  }
  bandwidthChart.setOption({
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: 'rgba(17,24,39,0.9)',
      borderColor: '#374151',
      textStyle: { color: '#e5e7eb' },
      formatter: function (params) {
        const p = params[0]
        const item = data[data.length - 1 - p.dataIndex] || {}
        let html = `${p.name}<br/>带宽利用率: <b style="color:${p.color}">${p.value}%</b>`
        if (item.interface) html += `<br/>接口: ${item.interface}`
        if (item.in_rate != null) {
          html += `<br/>下行 ${fmtRate(item.in_rate)} / 上行 ${fmtRate(item.out_rate)}`
        }
        return html
      }
    },
    grid: { left: '3%', right: '8%', top: '5%', bottom: '3%', containLabel: true },
    xAxis: {
      type: 'value',
      max: 100,
      axisLabel: { color: '#6b7280', fontSize: 10, formatter: '{value}%' },
      splitLine: { lineStyle: { color: '#1f2937' } }
    },
    yAxis: {
      type: 'category',
      data: data.map(d => d.name).reverse(),
      axisLabel: { color: '#9ca3af', fontSize: 10, width: 120, overflow: 'truncate' },
      axisLine: { lineStyle: { color: '#374151' } }
    },
    series: [{
      type: 'bar',
      data: data.map(d => d.bandwidth_usage).reverse(),
      barWidth: '50%',
      itemStyle: {
        borderRadius: [0, 4, 4, 0],
        color: function (params) {
          const val = params.value
          if (val >= 90) return '#ef4444'
          if (val >= 70) return '#f59e0b'
          if (val >= 50) return '#eab308'
          return '#10b981'
        }
      },
      label: {
        show: true,
        position: 'right',
        color: '#9ca3af',
        fontSize: 10,
        formatter: '{c}%'
      }
    }]
  })
}

function renderTopologyChart() {
  if (!topologyChartRef.value) return
  if (!topologyChart) {
    topologyChart = echarts.init(topologyChartRef.value)
  }
  const rawNodes = topologyData.value.nodes || []
  const rawEdges = topologyData.value.edges || []

  // 与拓扑发现页一致：状态色（在线绿/告警黄/离线红，不区分类型颜色）+ 状态图例 + 离线链路标红（大屏节点缩小）
  const nodes = buildTopologyNodes(rawNodes, 0.75, 'status', 'status')
  const links = buildTopologyEdges(rawEdges, rawNodes)

  const statusTextMap = { online: '在线', warning: '告警', offline: '离线' }

  topologyChart.setOption({
    tooltip: {
      backgroundColor: 'rgba(17,24,39,0.9)',
      borderColor: '#374151',
      textStyle: { color: '#e5e7eb' },
      formatter: function (params) {
        if (params.dataType === 'node' && params.data._rawData) {
          const d = params.data._rawData
          // 设备名为用户可写数据，渲染前转义防止存储型 XSS
          const name = String(d.name ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))
          const st = statusTextMap[d.status] || String(d.status ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))
          return `<b>${name}</b><br/>状态: ${st}`
        }
        if (params.dataType === 'edge' && params.data._hasOffline) {
          return '<b style="color:#ef4444">⚠ 该链路涉及离线设备</b>'
        }
        return ''
      }
    },
    legend: {
      data: ['在线', '告警', '离线'],
      textStyle: { color: '#9ca3af', fontSize: 11 },
      top: 0,
      right: 10
    },
    series: [{
      type: 'graph',
      layout: 'force',
      roam: true,
      draggable: true,
      force: {
        repulsion: 300,
        edgeLength: [80, 160],
        gravity: 0.08
      },
      label: { show: true },
      lineStyle: { color: '#374151', curveness: 0.1 },
      emphasis: {
        focus: 'adjacency',
        lineStyle: { width: 3, color: '#06b6d4' },
        itemStyle: { shadowBlur: 20 }
      },
      categories: [
        { name: '在线', itemStyle: { color: statusColors.online } },
        { name: '告警', itemStyle: { color: statusColors.warning } },
        { name: '离线', itemStyle: { color: statusColors.offline } }
      ],
      data: nodes.map(n => {
        const catMap = { online: 0, warning: 1, offline: 2 }
        return { ...n, category: catMap[n.category] || 0 }
      }),
      links: links
    }]
  }, true)
}

function renderAllCharts() {
  renderTypeChart()
  renderVendorChart()
  renderCpuChart()
  renderMemoryChart()
  renderBandwidthChart()
  renderTopologyChart()
}

// --- Alert Scrolling ---
function startAlertScroll() {
  if (scrollTimer) clearInterval(scrollTimer)
  scrollTimer = setInterval(() => {
    if (isScrollPaused) return
    const container = alertScrollRef.value
    if (!container || displayAlerts.value.length === 0) return
    scrollOffset += 1
    const itemHeight = 36
    const totalHeight = displayAlerts.value.length * itemHeight / 2
    if (scrollOffset >= totalHeight) {
      scrollOffset = 0
    }
    container.style.transform = `translateY(-${scrollOffset * 0.83}px)`
  }, 1000 / 30)
}

function pauseScroll() {
  isScrollPaused = true
}

function resumeScroll() {
  isScrollPaused = false
}

// --- Clock ---
function updateClock() {
  const now = new Date()
  const y = now.getFullYear()
  const m = String(now.getMonth() + 1).padStart(2, '0')
  const d = String(now.getDate()).padStart(2, '0')
  const h = String(now.getHours()).padStart(2, '0')
  const mi = String(now.getMinutes()).padStart(2, '0')
  const s = String(now.getSeconds()).padStart(2, '0')
  currentTime.value = `${y}-${m}-${d} ${h}:${mi}:${s}`
}

// --- Resize Handler ---
function handleResize() {
  typeChart && typeChart.resize()
  vendorChart && vendorChart.resize()
  cpuChart && cpuChart.resize()
  memoryChart && memoryChart.resize()
  bandwidthChart && bandwidthChart.resize()
  topologyChart && topologyChart.resize()
}

// --- Lifecycle ---
async function loadLicenseWarning() {
  try {
    const s = await getLicenseStatus()
    if (s.enabled && s.activated && s.locked) {
      licenseWarning.value = '平台授权已到期，功能已锁定，请前往授权管理激活'
    } else if (s.enabled && s.activated && !s.permanent && s.days_left !== null && s.days_left <= 30) {
      licenseWarning.value = `平台授权将于 ${(s.expires_at || '').slice(0, 10)} 到期（剩余 ${s.days_left} 天），请及时续期`
    } else {
      licenseWarning.value = ''
    }
  } catch (err) {
    licenseWarning.value = ''
  }
}

onMounted(async () => {
  updateClock()
  timeTimer = setInterval(updateClock, 1000)

  await fetchAllData()
  fetchBandwidth()  // 真实带宽采集较慢，独立异步加载，不阻塞主数据
  loadLicenseWarning()

  await nextTick()
  startAlertScroll()

  refreshTimer = setInterval(fetchAllData, 10000)
  bandwidthTimer = setInterval(fetchBandwidth, 30000)
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
  if (bandwidthTimer) clearInterval(bandwidthTimer)
  if (scrollTimer) clearInterval(scrollTimer)
  if (timeTimer) clearInterval(timeTimer)
  window.removeEventListener('resize', handleResize)
  typeChart && typeChart.dispose()
  vendorChart && vendorChart.dispose()
  cpuChart && cpuChart.dispose()
  memoryChart && memoryChart.dispose()
  bandwidthChart && bandwidthChart.dispose()
  topologyChart && topologyChart.dispose()
})
</script>

<style scoped>
.custom-scrollbar::-webkit-scrollbar {
  width: 4px;
}
.custom-scrollbar::-webkit-scrollbar-track {
  background: #1f2937;
  border-radius: 2px;
}
.custom-scrollbar::-webkit-scrollbar-thumb {
  background: #4b5563;
  border-radius: 2px;
}
.custom-scrollbar::-webkit-scrollbar-thumb:hover {
  background: #6b7280;
}
</style>
