<template>
  <div class="flex h-full flex-col">
    <header class="flex items-center justify-between px-4 pt-4">
      <h1 class="text-lg font-bold text-ink-strong">网络拓扑</h1>
      <button @click="load" class="rounded-lg border border-line px-3 py-1.5 text-xs text-ink-muted">刷新</button>
    </header>
    <div ref="chartRef" class="min-h-0 flex-1"></div>
    <p class="pb-2 text-center text-[10px] text-ink-faint">点击设备节点查看详情</p>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import * as echarts from 'echarts'
import { getTopology } from '../api.js'
import { chartTheme } from '../chartTheme.js'

const router = useRouter()
const chartRef = ref(null)
let chart = null

function buildOption(data) {
  const cc = chartTheme()
  const nodes = (data.nodes || []).map((n) => {
    const color = { online: '#10b981', warning: '#f59e0b', offline: '#ef4444' }[n.status] || '#64748b'
    return {
      id: String(n.id),
      name: n.name,
      symbolSize: n.type === 'core' ? 42 : n.type === 'router' ? 36 : 30,
      itemStyle: { color },
      label: { show: true, color: cc.label, fontSize: 10 },
    }
  })
  const links = (data.edges || []).map((e) => ({ source: String(e.source), target: String(e.target) }))
  return {
    backgroundColor: 'transparent',
    series: [{
      type: 'graph',
      layout: 'force',
      roam: true,
      draggable: true,
      force: { repulsion: 180, edgeLength: [80, 140], gravity: 0.1 },
      data: nodes,
      links,
      lineStyle: { color: cc.line, width: 1.5, curveness: 0.1 },
      emphasis: { focus: 'adjacency', lineStyle: { width: 3 } },
    }],
  }
}

async function load() {
  try {
    const res = await getTopology()
    const data = res?.data || res || { nodes: [], edges: [] }
    if (chart) chart.setOption(buildOption(data), true)
  } catch (e) { console.error(e) }
}

onMounted(async () => {
  await nextTick()
  if (chartRef.value) {
    chart = echarts.init(chartRef.value)
    chart.on('click', (params) => {
      if (params.dataType === 'node' && params.data && params.data.id) {
        const id = String(params.data.id)
        if (id && !id.startsWith('link-')) router.push(`/devices/${id}`)
      }
    })
    load()
    window.addEventListener('resize', onResize)
  }
})

function onResize() { if (chart) chart.resize() }

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  if (chart) { chart.dispose(); chart = null }
})
</script>
