/**
 * 拓扑渲染公共配置 - 拓扑发现页(Topology.vue)与监控大屏(Dashboard.vue)共用，
 * 保证两处节点的类型图例、状态边框、离线链路标红完全一致。
 */

export const statusColors = {
  online: '#10b981',
  warning: '#f59e0b',
  offline: '#ef4444'
}

export const nodeTypeConfig = {
  'switch': { symbol: 'SW', size: 42, symbolColor: '#34d399', label: '交换机' },
  'router': { symbol: 'R', size: 52, symbolColor: '#f472b6', label: '路由器' },
  'firewall': { symbol: 'FW', size: 44, symbolColor: '#fb923c', label: '防火墙' },
  'wireless': { symbol: 'AC', size: 44, symbolColor: '#22d3ee', label: '无线控制器' },
  'server': { symbol: 'SV', size: 40, symbolColor: '#22d3ee', label: '服务器' },
  'unknown': { symbol: '?', size: 36, symbolColor: '#9ca3af', label: '设备' }
}

export function getNodeTypeConfig(type) {
  return nodeTypeConfig[type] || nodeTypeConfig.unknown
}

export function getStatusColor(status) {
  return statusColors[status] || statusColors.offline
}

export function formatBandwidth(bandwidth) {
  if (bandwidth == null || bandwidth === '') return ''
  if (typeof bandwidth === 'number') {
    if (bandwidth >= 1000) return (bandwidth / 1000).toFixed(1) + ' Gbps'
    return bandwidth + ' Mbps'
  }
  return String(bandwidth)
}

/**
 * 构建 ECharts graph 节点数据。
 * - 节点主体色 = 设备类型色（与图例一致），外圈边框 = 状态色（在线绿/告警黄/离线红）
 * - colorMode='status' 时主体色也取状态色（拓扑发现页：初始在线全绿、有告警变黄、离线变红，不区分类型颜色）
 * - 节点中央显示类型符号，下方显示设备名
 * @param nodes 后端拓扑节点 [{id,name,type,status,...}]
 * @param scale 尺寸缩放（大屏用 0.75 缩小节点）
 * @param categoryMode 'type'（按类型分类）| 'status'（按状态分类，供大屏 legend 使用）
 * @param colorMode 'type'（主体=类型色）| 'status'（主体=状态色）
 */
export function buildTopologyNodes(nodes = [], scale = 1, categoryMode = 'type', colorMode = 'type') {
  return nodes.map((node) => {
    const typeCfg = getNodeTypeConfig(node.type)
    // 未标注状态时按在线处理，避免默认落到离线红
    const statusColor = getStatusColor(node.status || 'online')
    const bodyColor = colorMode === 'status' ? statusColor : typeCfg.symbolColor
    return {
      id: node.id != null ? String(node.id) : node.name,
      name: node.name,
      symbolSize: Math.round(typeCfg.size * scale),
      category: categoryMode === 'status' ? (node.status || 'online') : typeCfg.label,
      itemStyle: {
        color: bodyColor,
        borderColor: statusColor,
        borderWidth: 3,
        shadowBlur: 10,
        shadowColor: statusColor
      },
      label: {
        show: true,
        position: 'inside',
        formatter: `{type|${typeCfg.symbol}}\n{nm|${node.name}}`,
        rich: {
          type: {
            color: '#0f172a',
            fontSize: Math.round(13 * scale),
            fontWeight: 800,
            align: 'center',
            lineHeight: Math.round(15 * scale)
          },
          nm: {
            color: '#f3f4f6',
            fontSize: Math.round(9 * scale),
            align: 'center',
            lineHeight: Math.round(11 * scale),
            width: Math.round(96 * scale),
            overflow: 'truncate'
          }
        }
      },
      _rawData: node,
      _typeCfg: typeCfg
    }
  })
}

/**
 * 构建 ECharts graph 连线数据。
 * - 端点任一设备离线 → 红色加粗（覆盖其他配色）
 * - 自定义连线 → 青色；其余按 colorFn（如利用率配色）或默认灰色
 * @param edges 后端拓扑边 [{source,target,link_type,label,custom,bandwidth,utilization}]
 * @param nodes 节点列表（用于提取状态映射）
 * @param colorFn 可选：普通边的配色函数 (edge) => color
 */
export function buildTopologyEdges(edges = [], nodes = [], colorFn = null) {
  const nodeStatusMap = {}
  nodes.forEach((n) => { nodeStatusMap[String(n.id)] = n.status })
  return edges.map((edge) => {
    const hasOffline = nodeStatusMap[String(edge.source)] === 'offline'
      || nodeStatusMap[String(edge.target)] === 'offline'
    const isCustom = !!edge.custom
    const bandwidthText = formatBandwidth(edge.bandwidth)
    return {
      source: edge.source != null ? String(edge.source) : edge.source,
      target: edge.target != null ? String(edge.target) : edge.target,
      lineStyle: {
        width: hasOffline ? 3.5 : (isCustom ? 2.5 : 1.5),
        color: hasOffline ? '#ef4444' : (isCustom ? '#06b6d4' : (colorFn ? colorFn(edge) : '#374151')),
        curveness: 0.1,
        opacity: hasOffline ? 1 : (isCustom ? 0.95 : 0.8)
      },
      label: (isCustom || hasOffline || bandwidthText) ? {
        show: true,
        formatter: isCustom ? (edge.label || edge.link_type || '') : bandwidthText,
        fontSize: 9,
        color: hasOffline ? '#ef4444' : (isCustom ? '#22d3ee' : '#9ca3af'),
        backgroundColor: 'rgba(17, 24, 39, 0.8)',
        padding: [2, 4],
        borderRadius: 3
      } : undefined,
      _rawData: edge,
      _hasOffline: hasOffline
    }
  })
}
