<template>
  <div class="flex h-screen flex-col bg-slate-950 text-slate-100 animate-in">
    <!-- Header -->
    <div class="border-b border-slate-800 bg-slate-900/50 px-6 py-4">
      <div class="flex items-center justify-between">
        <div>
          <h1 class="text-2xl font-bold text-slate-100">网络拓扑可视化</h1>
          <p class="mt-1 text-sm text-slate-400">实时网络设备拓扑与链路状态</p>
        </div>
        <button
          @click="refreshTopology"
          :disabled="loading"
          class="rounded-lg border border-slate-700 bg-slate-800 px-4 py-2 text-sm font-medium text-slate-300 transition-colors hover:bg-slate-700 disabled:opacity-50"
        >
          {{ loading ? '加载中...' : '刷新' }}
        </button>
      </div>
    </div>

    <!-- Main Content Area -->
    <div class="flex flex-1 overflow-hidden">
      <!-- Chart Canvas -->
      <div class="relative flex-1">
        <div
          ref="chartRef"
          class="h-full w-full"
        />

        <!-- Loading Overlay -->
        <div
          v-if="loading"
          class="absolute inset-0 flex items-center justify-center bg-slate-950/60"
        >
          <div class="text-sm text-slate-400">正在加载拓扑数据...</div>
        </div>

        <!-- Empty State -->
        <div
          v-if="!loading && !hasData"
          class="absolute inset-0 flex items-center justify-center"
        >
          <div class="text-center">
            <div class="text-slate-500">暂无拓扑数据</div>
          </div>
        </div>
      </div>

      <!-- Side Panel: Legend -->
      <div class="w-72 border-l border-slate-800 bg-slate-900/50 p-4">
        <h3 class="mb-3 text-sm font-semibold text-slate-300">图例</h3>

        <!-- Node Type Legend（设备类型图例已移除，类型已直接应用于节点图形） -->

        <!-- Status Legend -->
        <div class="mb-5">
          <p class="mb-2 text-xs font-medium text-slate-500">设备状态</p>
          <div class="space-y-2">
            <div class="flex items-center gap-3">
              <div class="h-3 w-3 rounded-full" style="background-color: #10b981"></div>
              <span class="text-sm text-slate-300">在线 (Online)</span>
            </div>
            <div class="flex items-center gap-3">
              <div class="h-3 w-3 rounded-full" style="background-color: #f59e0b"></div>
              <span class="text-sm text-slate-300">告警 (Warning)</span>
            </div>
            <div class="flex items-center gap-3">
              <div class="h-3 w-3 rounded-full" style="background-color: #ef4444"></div>
              <span class="text-sm text-slate-300">离线 (Offline)</span>
            </div>
          </div>
        </div>

        <!-- Edge Legend -->
        <div class="mb-5">
          <p class="mb-2 text-xs font-medium text-slate-500">链路利用率</p>
          <div class="space-y-2">
            <div class="flex items-center gap-3">
              <div class="h-0.5 w-8" style="background-color: #6b7280"></div>
              <span class="text-sm text-slate-300">低 (&lt;50%)</span>
            </div>
            <div class="flex items-center gap-3">
              <div class="h-1 w-8" style="background-color: #f59e0b"></div>
              <span class="text-sm text-slate-300">中 (50-80%)</span>
            </div>
            <div class="flex items-center gap-3">
              <div class="h-1.5 w-8" style="background-color: #ef4444"></div>
              <span class="text-sm text-slate-300">高 (&gt;80%)</span>
            </div>
            <div class="flex items-center gap-3">
              <div class="h-0.5 w-8" style="background-color: #ef4444"></div>
              <span class="text-sm text-red-400">离线设备链路</span>
            </div>
          </div>
        </div>

        <!-- Custom Links -->
        <div>
          <p class="mb-2 text-xs font-medium text-slate-500">自定义连线</p>
          <div class="space-y-2">
            <select
              v-model="linkForm.sourceId"
              class="w-full rounded-lg border border-slate-700 bg-slate-800 px-2 py-1.5 text-xs text-slate-300 focus:outline-none focus:border-cyan-500"
            >
              <option value="">— 设备 A —</option>
              <option v-for="d in managedDevices" :key="d.id" :value="d.id">{{ d.name }} ({{ d.ip }})</option>
            </select>
            <select
              v-model="linkForm.targetId"
              class="w-full rounded-lg border border-slate-700 bg-slate-800 px-2 py-1.5 text-xs text-slate-300 focus:outline-none focus:border-cyan-500"
            >
              <option value="">— 设备 B —</option>
              <option v-for="d in managedDevices" :key="d.id" :value="d.id">{{ d.name }} ({{ d.ip }})</option>
            </select>
            <select
              v-model="linkForm.linkType"
              class="w-full rounded-lg border border-slate-700 bg-slate-800 px-2 py-1.5 text-xs text-slate-300 focus:outline-none focus:border-cyan-500"
            >
              <option value="custom">自定义链路</option>
              <option value="fiber">光纤直连</option>
              <option value="ethernet">以太网</option>
              <option value="trunk">Trunk 链路</option>
              <option value="wan">WAN 专线</option>
            </select>
            <button
              @click="addLink"
              :disabled="!linkForm.sourceId || !linkForm.targetId || linkForm.sourceId === linkForm.targetId"
              class="w-full rounded-lg bg-cyan-600 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-cyan-500 disabled:opacity-40"
            >
              ➕ 添加连线
            </button>
          </div>
          <div v-if="customLinks.length" class="mt-3 space-y-1.5 max-h-44 overflow-y-auto custom-scrollbar">
            <div
              v-for="l in customLinks"
              :key="l.id"
              class="flex items-center justify-between gap-2 rounded-lg bg-slate-800/60 px-2 py-1.5 text-xs"
            >
              <span class="text-slate-300 truncate">{{ l.source_name }} ↔ {{ l.target_name }}</span>
              <button
                @click="removeLink(l.id)"
                class="shrink-0 rounded px-1 text-red-400 hover:bg-red-500/20 hover:text-red-300"
                title="删除连线"
              >✕</button>
            </div>
          </div>
          <div v-else class="mt-3 text-xs text-slate-600">暂无自定义连线</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts'
import { getTopology, getDevices, getTopologyLinks, createTopologyLink, deleteTopologyLink } from '../api/index.js'
import { buildTopologyNodes, buildTopologyEdges, getStatusColor } from '../utils/topologyConfig.js'

const router = useRouter()

// HTML 转义：tooltip 渲染后端/用户数据前必须转义，防止存储型 XSS
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))
}

const chartRef = ref(null)
const loading = ref(false)
const hasData = ref(false)
let chartInstance = null
let resizeObserver = null

// 自定义连线状态
const managedDevices = ref([])
const customLinks = ref([])
const linkForm = ref({ sourceId: '', targetId: '', linkType: 'custom' })

const getEdgeColor = (utilization) => {
  if (utilization >= 80) return '#ef4444'
  if (utilization >= 50) return '#f59e0b'
  return '#6b7280'
}

const formatBandwidth = (bandwidth) => {
  if (bandwidth >= 1000) return (bandwidth / 1000).toFixed(1) + ' Gbps'
  return bandwidth + ' Mbps'
}

const buildChartOption = (data) => {
  // 节点/边统一使用公共构建函数（状态色：在线绿/告警黄/离线红，不区分设备类型颜色；离线链路标红）
  const nodes = buildTopologyNodes(data.nodes || [], 1, 'type', 'status')
  const edges = buildTopologyEdges(data.edges || [], data.nodes || [], (edge) => getEdgeColor(edge.utilization))

  return {
    backgroundColor: 'transparent',
    tooltip: {
      show: true,
      backgroundColor: 'rgba(17, 24, 39, 0.95)',
      borderColor: '#374151',
      borderWidth: 1,
      padding: 12,
      textStyle: {
        color: '#f3f4f6',
        fontSize: 13
      },
      formatter: (params) => {
        if (params.dataType === 'node' && params.data._rawData) {
          const d = params.data._rawData
          const statusText = {
            online: '在线',
            warning: '告警',
            offline: '离线'
          }
          const statusColor = getStatusColor(d.status)
          const typeCfg = params.data._typeCfg
          return `
            <div style="font-weight:600;font-size:14px;margin-bottom:8px;color:#f3f4f6;">${esc(d.name)}</div>
            <div style="display:grid;grid-template-columns:auto auto;gap:4px 12px;font-size:12px;">
              <span style="color:#9ca3af;">类型:</span><span style="color:#e5e7eb;">${esc(typeCfg.label)}</span>
              <span style="color:#9ca3af;">IP地址:</span><span style="color:#e5e7eb;font-family:monospace;">${esc(d.ip)}</span>
              <span style="color:#9ca3af;">厂商:</span><span style="color:#e5e7eb;">${esc(d.vendor)}</span>
              <span style="color:#9ca3af;">状态:</span><span style="color:${statusColor};font-weight:600;">${statusText[d.status] || esc(d.status)}</span>
              <span style="color:#9ca3af;">CPU:</span><span style="color:${d.cpu >= 80 ? '#ef4444' : d.cpu >= 60 ? '#f59e0b' : '#10b981'};">${d.cpu}%</span>
              <span style="color:#9ca3af;">内存:</span><span style="color:${d.memory >= 80 ? '#ef4444' : d.memory >= 60 ? '#f59e0b' : '#10b981'};">${d.memory}%</span>
            </div>
            <div style="font-size:11px;color:#22d3ee;margin-top:6px;">🖱 双击设备查看详情</div>
          `
        }
        if (params.dataType === 'edge' && params.data._rawData) {
          const d = params.data._rawData
          if (d.custom) {
            const offlineTip = d._hasOffline ? '<div style="font-size:12px;color:#ef4444;font-weight:600;margin-top:4px;">⚠ 该链路涉及离线设备</div>' : ''
            return `
              <div style="font-weight:600;font-size:13px;margin-bottom:6px;">自定义连线</div>
              <div style="font-size:12px;color:#9ca3af;">类型: <span style="color:#22d3ee;">${esc(d.link_type || 'custom')}</span></div>
              ${d.label ? `<div style="font-size:12px;color:#9ca3af;">备注: <span style="color:#e5e7eb;">${esc(d.label)}</span></div>` : ''}
              ${offlineTip}
            `
          }
          const offlineTip = d._hasOffline ? '<div style="font-size:12px;color:#ef4444;font-weight:600;margin-top:4px;">⚠ 该链路涉及离线设备</div>' : ''
          return `
            <div style="font-weight:600;font-size:13px;margin-bottom:6px;">链路信息</div>
            <div style="font-size:12px;color:#9ca3af;">带宽: <span style="color:#e5e7eb;">${formatBandwidth(d.bandwidth)}</span></div>
            <div style="font-size:12px;color:#9ca3af;">利用率: <span style="color:${getEdgeColor(d.utilization)};">${d.utilization}%</span></div>
            ${offlineTip}
          `
        }
        return ''
      }
    },
    series: [
      {
        type: 'graph',
        layout: 'force',
        roam: true,
        draggable: true,
        force: {
          repulsion: 300,
          edgeLength: [120, 200],
          gravity: 0.08,
          layoutAnimation: true
        },
        emphasis: {
          focus: 'adjacency',
          lineStyle: {
            width: 5,
            opacity: 1
          },
          itemStyle: {
            borderWidth: 3,
            shadowBlur: 20
          },
          label: {
            fontSize: 14,
            fontWeight: 700
          }
        },
        data: nodes,
        links: edges,
        lineStyle: {
          curveness: 0.1
        }
      }
    ]
  }
}

const renderChart = (data) => {
  if (!chartInstance) return
  hasData.value = (data.nodes && data.nodes.length > 0)
  if (!hasData.value) {
    chartInstance.clear()
    return
  }
  const option = buildChartOption(data)
  chartInstance.setOption(option, true)
}

const fetchTopology = async () => {
  loading.value = true
  try {
    const res = await getTopology()
    const topologyData = res?.data || res || { nodes: [], edges: [] }
    renderChart(topologyData)
  } catch (error) {
    console.error('获取拓扑数据失败:', error)
    hasData.value = false
  } finally {
    loading.value = false
  }
}

const refreshTopology = () => {
  fetchTopology()
}

// ---- 自定义连线 ----
const fetchManagedDevices = async () => {
  try {
    const res = await getDevices({ page: 1, page_size: 300 })
    const items = res?.data?.items || res?.items || []
    managedDevices.value = items.map((d) => ({ id: d.id, name: d.name || d.ip, ip: d.ip }))
  } catch (err) {
    console.error('Fetch devices for link error:', err)
  }
}

const fetchLinks = async () => {
  try {
    const res = await getTopologyLinks()
    customLinks.value = Array.isArray(res) ? res : (res?.data || [])
  } catch (err) {
    console.error('Fetch links error:', err)
  }
}

const addLink = async () => {
  if (!linkForm.value.sourceId || !linkForm.value.targetId) return
  if (linkForm.value.sourceId === linkForm.value.targetId) return
  try {
    await createTopologyLink({
      source_device_id: Number(linkForm.value.sourceId),
      target_device_id: Number(linkForm.value.targetId),
      link_type: linkForm.value.linkType,
    })
    linkForm.value.sourceId = ''
    linkForm.value.targetId = ''
    await Promise.all([fetchLinks(), fetchTopology()])
  } catch (err) {
    const msg = err?.response?.data?.detail || '添加连线失败'
    alert(msg)
  }
}

const removeLink = async (linkId) => {
  try {
    await deleteTopologyLink(linkId)
    await Promise.all([fetchLinks(), fetchTopology()])
  } catch (err) {
    alert('删除连线失败')
  }
}

const handleResize = () => {
  if (chartInstance) {
    chartInstance.resize()
  }
}

onMounted(async () => {
  await nextTick()
  if (chartRef.value) {
    chartInstance = echarts.init(chartRef.value, null, {
      renderer: 'canvas'
    })
    // 双击设备节点 → 跳转设备详情页（节点 id 即设备 id）
    chartInstance.on('dblclick', (params) => {
      if (params.dataType !== 'node' || !params.data) return
      const raw = params.data._rawData
      if (!raw || raw.id == null) return
      const deviceId = String(raw.id)
      if (!deviceId || deviceId.startsWith('link-')) return
      router.push({ name: 'DeviceDetail', params: { id: deviceId } })
    })
    if (typeof ResizeObserver !== 'undefined') {
      resizeObserver = new ResizeObserver(handleResize)
      resizeObserver.observe(chartRef.value)
    }
  }
  fetchManagedDevices()
  fetchLinks()
  fetchTopology()
})

onBeforeUnmount(() => {
  if (resizeObserver) {
    resizeObserver.disconnect()
    resizeObserver = null
  }
  if (chartInstance) {
    chartInstance.dispose()
    chartInstance = null
  }
})
</script>
