<template>
  <div class="min-h-screen bg-app text-ink-strong p-4 flex flex-col gap-4 animate-in">
    <div class="flex items-center justify-between px-2">
      <div class="flex items-center gap-3">
        <div class="w-2 h-8 bg-gradient-to-b from-green-400 to-teal-600 rounded-full"></div>
        <h1 class="text-2xl font-bold tracking-wide">等保合规管理</h1>
      </div>
      <div class="flex items-center gap-2">
        <button
          @click="showExamples = true"
          class="px-4 py-2 text-sm rounded-lg bg-hover/40 text-ink-muted border border-line-strong/50 hover:bg-hover/60 transition-colors"
        >
          合规示例（H3C）
        </button>
        <button
          @click="runCheckAll"
          :disabled="checking"
          class="px-4 py-2 text-sm rounded-lg bg-green-500/15 text-green-400 border border-green-500/30 hover:bg-green-500/25 transition-colors disabled:opacity-50"
        >
          {{ checking ? '评估中...' : '评估全部设备' }}
        </button>
        <button
          @click="runCheckSelected"
          :disabled="checking || selectedIds.size === 0"
          class="px-4 py-2 text-sm rounded-lg bg-cyan-500/15 text-cyan-400 border border-cyan-500/30 hover:bg-cyan-500/25 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {{ checking ? '评估中...' : `评估选中设备 (${selectedIds.size})` }}
        </button>
      </div>
    </div>

    <!-- 评估方式说明 -->
    <div class="px-2 -mt-1 text-xs text-ink-faint">
      <span class="inline-flex items-center gap-1 mr-4">
        <span class="w-2 h-2 rounded-full bg-cyan-400"></span> SSH 真实配置核查（等保二级交换机）
      </span>
      <span class="inline-flex items-center gap-1">
        <span class="w-2 h-2 rounded-full bg-ink-faint"></span> 平台指标推断（设备未配置 SSH 时回退）
      </span>
    </div>

    <!-- Overall Score + Radar Chart -->
    <div class="grid grid-cols-3 gap-4 h-48">
      <div class="bg-surface rounded-xl border border-line p-4 flex flex-col items-center justify-center">
        <span class="text-xs text-ink-muted mb-1">整体合规评分</span>
        <span class="text-5xl font-bold" :class="scoreColor">{{ overallScore }}%</span>
        <span class="text-xs text-ink-faint mt-1">{{ overallStats.compliant_devices || 0 }} / {{ overallStats.total_devices || 0 }} 设备合规</span>
      </div>
      <div class="col-span-2 bg-surface rounded-xl border border-line p-4 flex flex-col">
        <div class="flex items-center gap-2 border-l-4 border-green-400 pl-2 mb-2">
          <span class="text-sm font-semibold text-ink">五大类合规得分</span>
        </div>
        <div ref="radarChartRef" class="flex-1 w-full"></div>
      </div>
    </div>

    <!-- Device Compliance Table -->
    <div class="bg-surface rounded-xl border border-line overflow-hidden flex-1">
      <div class="flex items-center justify-between px-4 py-3 border-b border-line">
        <div class="flex items-center gap-2">
          <span class="w-2.5 h-2.5 bg-green-400 rounded-full"></span>
          <h3 class="text-sm font-semibold">设备合规状态</h3>
          <span class="text-xs text-ink-faint">({{ devices.length }})</span>
        </div>
        <div class="flex items-center gap-3 text-xs text-ink-muted">
          <label class="flex items-center gap-1.5 cursor-pointer hover:text-ink">
            <input type="checkbox" :checked="allSelected" @change="toggleAll" class="accent-cyan-500" />
            全选
          </label>
          <button @click="clearSelection" class="hover:text-ink">清空</button>
        </div>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-line text-ink-muted text-xs">
              <th class="text-left px-4 py-2.5 font-medium w-8">
                <input type="checkbox" :checked="allSelected" @change="toggleAll" class="accent-cyan-500" />
              </th>
              <th class="text-left px-4 py-2.5 font-medium">设备</th>
              <th class="text-left px-4 py-2.5 font-medium">评估方式</th>
              <th class="text-left px-4 py-2.5 font-medium">合规评分</th>
              <th class="text-left px-4 py-2.5 font-medium">合规检查项</th>
              <th class="text-left px-4 py-2.5 font-medium">最后检查时间</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="device in devices" :key="deviceKey(device)">
              <tr
                class="border-b border-line/50 hover:bg-hover/30 transition-colors cursor-pointer"
                @click="toggleExpand(deviceKey(device))"
              >
                <td class="px-4 py-2.5" @click.stop>
                  <input
                    type="checkbox"
                    :checked="selectedIds.has(deviceKey(device))"
                    @change="toggleSelect(deviceKey(device))"
                    class="accent-cyan-500"
                  />
                </td>
                <td class="px-4 py-2.5">
                  <span class="text-ink-faint text-xs transition-transform inline-block mr-1" :class="expandedId === deviceKey(device) ? 'rotate-90' : ''">&#9654;</span>
                  <div class="inline-flex flex-col align-middle">
                    <span class="text-ink">{{ device.device_name }}</span>
                    <span class="text-xs text-ink-faint font-mono">{{ device.ip || '-' }}</span>
                  </div>
                </td>
                <td class="px-4 py-2.5">
                  <span
                    class="px-2 py-0.5 rounded text-xs font-medium"
                    :class="device.method === 'ssh_config' ? 'bg-cyan-500/15 text-cyan-400 border border-cyan-500/30' : 'bg-line-strong/20 text-ink-muted border border-line-strong/30'"
                  >
                    {{ device.method === 'ssh_config' ? 'SSH 核查' : (device.method === 'snmp_fallback' ? '指标推断' : '待评估') }}
                  </span>
                </td>
                <td class="px-4 py-2.5">
                  <span class="px-2 py-0.5 rounded text-xs font-medium" :class="complianceScoreBadge(device.score != null ? device.score : device.compliance_score)">{{ device.score != null ? device.score : device.compliance_score }}%</span>
                </td>
                <td class="px-4 py-2.5 text-ink-muted">{{ device.passed != null ? device.passed : (device.passed_checks || 0) }} / {{ device.total != null ? device.total : (device.total_checks || 0) }}</td>
                <td class="px-4 py-2.5 text-ink-faint text-xs">{{ formatTime(device.checked_at || device.last_checked) }}</td>
              </tr>
              <!-- Expanded: Control Checks Detail -->
              <tr v-if="expandedId === deviceKey(device)" class="bg-surface-2/30">
                <td colspan="6" class="px-6 py-3">
                  <div v-if="expandedLoading" class="text-ink-faint text-sm py-4 text-center">加载中...</div>
                  <div v-else-if="expandedChecks.length === 0" class="text-ink-faint text-sm py-4 text-center">暂无检查项数据</div>
                  <div v-else>
                    <!-- 分类得分 -->
                    <div v-if="expandedCategories && Object.keys(expandedCategories).length" class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2 mb-4">
                      <div
                        v-for="(cat, key) in expandedCategories"
                        :key="key"
                        class="bg-surface/60 rounded-lg px-3 py-2 border border-line/50"
                      >
                        <div class="text-xs text-ink-muted">{{ cat.label }}</div>
                        <div class="text-sm font-semibold mt-0.5" :class="scoreColorClass(cat.score)">{{ cat.score }}%</div>
                      </div>
                    </div>
                    <div class="grid grid-cols-1 gap-2 max-h-60 overflow-y-auto custom-scrollbar">
                      <div
                        v-for="check in expandedChecks"
                        :key="check.id || check.control_id"
                        class="flex items-start justify-between gap-3 bg-surface/60 rounded-lg px-4 py-2 border border-line/50"
                      >
                        <div class="flex items-start gap-3 min-w-0">
                          <span class="w-2 h-2 rounded-full mt-1.5 shrink-0" :class="checkStatusDot(check.status)"></span>
                          <div class="min-w-0">
                            <div class="text-sm text-ink-muted">{{ check.desc || check.name || check.control_name }}</div>
                            <div v-if="check.evidence" class="text-xs text-ink-faint mt-0.5 break-words">{{ check.evidence }}</div>
                          </div>
                        </div>
                        <span class="text-xs px-2 py-0.5 rounded font-medium shrink-0" :class="checkStatusBadge(check.status)">{{ checkStatusLabel(check.status) }}</span>
                      </div>
                    </div>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
        <div v-if="devices.length === 0" class="px-4 py-10 text-center text-ink-faint text-sm">暂无设备合规数据</div>
        <!-- 分页控件 -->
        <div v-if="complianceTotal > compliancePageSize" class="flex items-center justify-between px-4 py-3 border-t border-line">
          <span class="text-xs text-ink-faint">共 {{ complianceTotal }} 台设备</span>
          <div class="flex items-center gap-3 text-sm">
            <span class="flex items-center gap-1 text-xs text-ink-faint">
              每页
              <select
                v-model="compliancePageSize"
                @change="onCompliancePageSizeChange"
                class="bg-surface-2 border border-line rounded-lg px-2 py-1 text-sm text-ink focus:outline-none"
              >
                <option :value="20">20</option>
                <option :value="50">50</option>
                <option :value="100">100</option>
              </select>
              条
            </span>
            <button
              @click="compliancePage--; fetchStatus()"
              :disabled="compliancePage <= 1"
              class="px-3 py-1 rounded-lg bg-surface-2 text-ink-muted border border-line hover:bg-hover disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >上一页</button>
            <span class="text-xs text-ink-muted">第 {{ compliancePage }} / {{ complianceTotalPages }} 页</span>
            <button
              @click="compliancePage++; fetchStatus()"
              :disabled="compliancePage >= complianceTotalPages"
              class="px-3 py-1 rounded-lg bg-surface-2 text-ink-muted border border-line hover:bg-hover disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >下一页</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 合规示例弹窗（H3C 配置） -->
    <div
      v-if="showExamples"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      @click.self="showExamples = false"
    >
      <div class="bg-surface border border-line rounded-xl w-[820px] max-h-[85vh] flex flex-col shadow-2xl">
        <div class="flex items-center justify-between px-6 py-4 border-b border-line">
          <div>
            <h3 class="text-lg font-bold">等保合规配置示例（H3C 设备）</h3>
            <p class="text-xs text-ink-faint mt-0.5">对应平台合规检查项 SEC-1.1 ~ SEC-5.1，可直接复制到设备执行（需在系统视图下）</p>
          </div>
          <button class="px-3 py-1.5 text-xs text-ink-muted hover:text-ink" @click="showExamples = false">关闭</button>
        </div>
        <div class="flex-1 overflow-auto custom-scrollbar p-6 space-y-6">
          <div v-for="group in complianceExamples" :key="group.category">
            <div class="flex items-center gap-2 mb-3">
              <span class="text-sm font-semibold" :class="group.color">{{ group.category }}</span>
              <span class="text-[10px] px-1.5 py-0.5 rounded bg-surface-2 text-ink-faint">{{ group.items.length }} 项</span>
              <div class="flex-1 h-px bg-surface-2"></div>
            </div>
            <div class="space-y-3">
              <div v-for="item in group.items" :key="item.id" class="bg-surface-2/40 border border-line/60 rounded-lg p-3">
                <div class="flex items-start justify-between gap-3 mb-1.5">
                  <div class="flex items-center gap-2">
                    <span class="text-[10px] px-1.5 py-0.5 rounded bg-blue-900/50 text-blue-300 font-mono">{{ item.id }}</span>
                    <span class="text-sm text-ink">{{ item.desc }}</span>
                  </div>
                  <button
                    class="px-2 py-0.5 text-[10px] bg-hover hover:bg-line-strong rounded text-ink-muted shrink-0 transition-colors"
                    @click="copyExample(item.cmd)"
                  >
                    复制
                  </button>
                </div>
                <pre class="text-xs text-green-300/90 font-mono whitespace-pre-wrap leading-relaxed bg-app/70 rounded p-2.5 overflow-x-auto">{{ item.cmd }}</pre>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { chartTheme } from '../utils/chartTheme'
const cc = chartTheme()
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import * as echarts from 'echarts'

const API_BASE = '/api/v1'

// === 合规示例（H3C 设备配置）===
const showExamples = ref(false)
const complianceExamples = [
  {
    category: '身份鉴别', color: 'text-blue-400',
    items: [
      {
        id: 'SEC-1.1', desc: '密码复杂度与有效期策略',
        cmd: 'password-control enable\npassword-control length 8\npassword-control complexity user-name check\npassword-control composition type-number 3 type-length 1\npassword-control aging 90',
      },
      {
        id: 'SEC-1.2', desc: '登录失败次数限制与锁定',
        cmd: 'password-control login-attempt 3 exceed lock-time 5',
      },
      {
        id: 'SEC-1.3', desc: '远程管理空闲超时',
        cmd: 'user-interface vty 0 4\n  idle-timeout 5 0',
      },
      {
        id: 'SEC-1.4', desc: 'VTY 线路使用 AAA 认证',
        cmd: 'user-interface vty 0 4\n  authentication-mode scheme',
      },
    ],
  },
  {
    category: '访问控制', color: 'text-cyan-400',
    items: [
      {
        id: 'SEC-2.1', desc: '管理源地址限制（VTY 应用 ACL）',
        cmd: 'acl basic 2001\n  rule 5 permit source 10.0.0.0 0.0.0.255\n  rule 10 deny source any\nuser-interface vty 0 4\n  acl 2001 inbound',
      },
      {
        id: 'SEC-2.2', desc: '账户权限分离（管理/审计角色分离）',
        cmd: 'local-user admin\n  service-type ssh\n  authorization-attribute user-role network-admin\nlocal-user audit\n  service-type ssh\n  authorization-attribute user-role network-operator',
      },
      {
        id: 'SEC-2.3', desc: '未使用端口 shutdown',
        cmd: 'interface GigabitEthernet1/0/10\n  shutdown',
      },
    ],
  },
  {
    category: '安全审计', color: 'text-green-400',
    items: [
      {
        id: 'SEC-3.1', desc: '信息中心/日志功能启用',
        cmd: 'info-center enable\ninfo-center logbuffer size 1024',
      },
      {
        id: 'SEC-3.2', desc: '配置远程日志服务器',
        cmd: 'info-center loghost 10.0.0.2\ninfo-center source default loghost log-level informational',
      },
      {
        id: 'SEC-3.3', desc: '审计日志包含登录/配置变更记录',
        cmd: 'info-center source default loghost log-level informational\ninfo-center logbuffer size 1024\n# 审计日志默认记录 LOGIN/CFG 事件，无需额外开启',
      },
    ],
  },
  {
    category: '入侵防范', color: 'text-orange-400',
    items: [
      {
        id: 'SEC-4.1', desc: '关闭 Telnet，仅保留 SSH 管理',
        cmd: 'undo telnet server enable\nssh server enable',
      },
      {
        id: 'SEC-4.2', desc: 'DHCP Snooping / ARP 防攻击 / IP Source Guard',
        cmd: 'dhcp snooping enable\ninterface GigabitEthernet1/0/1\n  dhcp snooping trust\ninterface GigabitEthernet1/0/2\n  dhcp snooping trust\narp anti-attack rate-limit enable\n# 需要端口安全时启用 ip source guard（需先配置 DHCP 绑定）',
      },
    ],
  },
  {
    category: '数据保密性', color: 'text-purple-400',
    items: [
      {
        id: 'SEC-5.1', desc: 'SNMP 使用 v3（加密通信）',
        cmd: 'snmp-agent sys-info version v3\nsnmp-agent group v3 v3group privacy\nsnmp-agent usm-user v3 aiops v3group privacy cipher simple <你的密码>',
      },
    ],
  },
]

// 复制示例命令（http://IP 非安全上下文需 execCommand 兜底）
function copyExample(text) {
  const useClipboard = navigator.clipboard && window.isSecureContext
  if (useClipboard) {
    navigator.clipboard.writeText(text)
      .then(() => alert('已复制到剪贴板'))
      .catch(() => execCopy(text))
  } else {
    execCopy(text)
  }
}
function execCopy(text) {
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.top = '0'
    ta.style.left = '0'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.focus()
    ta.select()
    ta.setSelectionRange(0, text.length)
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    alert(ok ? '已复制到剪贴板' : '复制失败，请手动选中复制')
  } catch (e) {
    alert('复制失败，请手动选中复制')
  }
}

const overallScore = ref(0)
const overallStats = ref({})
const devices = ref([])
// 设备列表分页
const compliancePage = ref(1)
const compliancePageSize = ref(20)
const complianceTotal = ref(0)
const complianceTotalPages = computed(() =>
  Math.max(1, Math.ceil(complianceTotal.value / compliancePageSize.value))
)
const expandedId = ref(null)
const expandedChecks = ref([])
const expandedCategories = ref(null)
const expandedLoading = ref(false)
const checking = ref(false)
const selectedIds = ref(new Set())

const radarChartRef = ref(null)
let radarChart = null

const allSelected = computed(() => {
  return devices.value.length > 0 && devices.value.every((d) => selectedIds.value.has(deviceKey(d)))
})

const scoreColor = computed(() => {
  const s = overallScore.value
  if (s >= 90) return 'text-green-400'
  if (s >= 70) return 'text-yellow-400'
  return 'text-red-400'
})

function scoreColorClass(score) {
  const s = score || 0
  if (s >= 90) return 'text-green-400'
  if (s >= 70) return 'text-yellow-400'
  return 'text-red-400'
}

function complianceScoreBadge(score) {
  const s = score || 0
  if (s >= 90) return 'bg-green-500/15 text-green-400'
  if (s >= 70) return 'bg-yellow-500/15 text-yellow-400'
  return 'bg-red-500/15 text-red-400'
}

function checkStatusDot(status) {
  const map = { compliant: 'bg-green-500', partial: 'bg-yellow-500', non_compliant: 'bg-red-500', not_applicable: 'bg-ink-faint' }
  return map[status] || 'bg-ink-faint'
}

function checkStatusBadge(status) {
  const map = {
    compliant: 'bg-green-500/15 text-green-400 border-green-500/30',
    partial: 'bg-yellow-500/15 text-yellow-400 border-yellow-500/30',
    non_compliant: 'bg-red-500/15 text-red-400 border-red-500/30',
    not_applicable: 'bg-line-strong/20 text-ink-muted border-line-strong/30',
  }
  return (map[status] || 'bg-line-strong/20 text-ink-muted') + ' border'
}

function checkStatusLabel(status) {
  const map = { compliant: '合规', partial: '部分合规', non_compliant: '不合规', not_applicable: '不适用' }
  return map[status] || status
}

function toggleAll(e) {
  if (e.target.checked) {
    const set = new Set()
    devices.value.forEach((d) => set.add(deviceKey(d)))
    selectedIds.value = set
  } else {
    selectedIds.value = new Set()
  }
}

function clearSelection() {
  selectedIds.value = new Set()
}

function toggleSelect(id) {
  const set = new Set(selectedIds.value)
  if (set.has(id)) set.delete(id)
  else set.add(id)
  selectedIds.value = set
}

function deviceKey(device) {
  if (device.device_id != null) return device.device_id
  if (device.id != null) return device.id
  return device.device_name || device.name || JSON.stringify(device)
}

function formatTime(value) {
  if (!value) return '-'
  return String(value).slice(0, 19).replace('T', ' ')
}

async function fetchStatus() {
  try {
    const params = new URLSearchParams({ page: compliancePage.value, page_size: compliancePageSize.value })
    const res = await fetch(`${API_BASE}/compliance/status?${params}`)
    const data = await res.json()
    const d = data.data || data
    devices.value = d.devices || []
    overallScore.value = d.overall_score || 0
    complianceTotal.value = d.total_devices || devices.value.length
    overallStats.value = {
      total_devices: d.total_devices || devices.value.length,
      compliant_devices: d.compliant_devices || 0,
      categories: d.categories || {},
    }
    // 当前页超出总页数时（如删除设备/切换每页条数后）回退到最后一页
    if (compliancePage.value > complianceTotalPages.value) {
      compliancePage.value = complianceTotalPages.value
      return fetchStatus()
    }
    await nextTick()
    renderRadarChart()
  } catch (err) {
    console.error('Compliance status fetch error:', err)
  }
}

function onCompliancePageSizeChange() {
  compliancePage.value = 1
  fetchStatus()
}

function renderRadarChart() {
  if (!radarChartRef.value) return
  if (!radarChart) radarChart = echarts.init(radarChartRef.value)

  // 从已展开设备或默认构造五大类
  const cats = expandedCategories.value || overallStats.value.categories || {}
  const defaultCats = { identity_auth: 0, access_control: 0, security_audit: 0, intrusion_prevention: 0, data_confidentiality: 0 }
  const merged = { ...defaultCats, ...cats }
  const labels = { identity_auth: '身份鉴别', access_control: '访问控制', security_audit: '安全审计', intrusion_prevention: '入侵防范', data_confidentiality: '数据保密性' }

  const indicators = Object.keys(merged).map((key) => ({ name: labels[key] || key, max: 100 }))
  const values = Object.keys(merged).map((key) => merged[key])

  radarChart.setOption({
    color: ['#10b981'],
    tooltip: { backgroundColor: cc.tooltipBg, borderColor: cc.tooltipBorder, textStyle: { color: cc.text } },
    legend: { bottom: 0, textStyle: { color: cc.sub, fontSize: 10 } },
    radar: {
      center: ['50%', '45%'],
      radius: '65%',
      indicator: indicators,
      axisName: { color: cc.sub, fontSize: 10 },
      splitArea: { areaStyle: { color: ['transparent'] } },
      splitLine: { lineStyle: { color: cc.split } },
      axisLine: { lineStyle: { color: cc.tooltipBorder } },
    },
    series: [{
      type: 'radar',
      data: [{ value: values, name: '当前合规水平', areaStyle: { color: 'rgba(16,185,129,0.15)' } }],
      symbol: 'circle',
      symbolSize: 4,
      lineStyle: { width: 2 },
    }],
  }, true)
}

async function toggleExpand(deviceId) {
  if (expandedId.value === deviceId) {
    expandedId.value = null
    expandedChecks.value = []
    expandedCategories.value = null
    return
  }
  expandedId.value = deviceId
  expandedLoading.value = true
  try {
    const device = devices.value.find((d) => deviceKey(d) === deviceId)
    // 优先用评估结果里的 details/categories（已含在批量评估返回中）
    if (device && device.details) {
      expandedChecks.value = device.details
      expandedCategories.value = device.categories || null
    } else {
      const res = await fetch(`${API_BASE}/compliance/score/${deviceId}`)
      const data = await res.json()
      const d = data.data || data
      expandedChecks.value = d.checks || d.details || d || []
      expandedCategories.value = d.categories || null
    }
    await nextTick()
    renderRadarChart()
  } catch (err) {
    console.error('Score detail fetch error:', err)
  } finally {
    expandedLoading.value = false
  }
}

async function runCheckAll() {
  await runCheck({})
}

async function runCheckSelected() {
  const ids = [...selectedIds.value]
  if (ids.length === 0) return
  await runCheck({ device_ids: ids })
}

async function runCheck(body) {
  checking.value = true
  try {
    const res = await fetch(`${API_BASE}/compliance/check`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    const data = await res.json()
    const d = data.data || data
    if (d.devices) {
      // 批量结果：按 device_id 合并到现有列表（更新已有行，不新增重复行）
      const byId = new Map()
      devices.value.forEach((dev) => {
        const key = dev.device_id != null ? dev.device_id : dev.id
        if (key != null) byId.set(key, dev)
      })
      d.devices.forEach((dev) => {
        const key = dev.device_id != null ? dev.device_id : dev.id
        if (key == null) return
        const prev = byId.get(key)
        // 保留原行未变更字段（如 device_type），用评估结果覆盖指标与时间
        byId.set(key, { ...(prev || {}), ...dev, device_id: key, id: key, device_name: dev.device_name || (prev && prev.device_name) })
      })
      devices.value = [...byId.values()]
      overallScore.value = d.overall_avg || 0
      overallStats.value = {
        total_devices: devices.value.length,
        compliant_devices: devices.value.filter((dev) => (dev.score != null ? dev.score : dev.compliance_score) >= 90).length,
        categories: aggregateCategories(d.devices),
      }
    } else if (d.details) {
      // 单设备结果：更新已有行
      const key = d.device_id != null ? d.device_id : d.id
      const idx = devices.value.findIndex((dev) => ((dev.device_id != null ? dev.device_id : dev.id) === key))
      if (idx >= 0) {
        devices.value[idx] = { ...devices.value[idx], ...d, device_id: key, id: key }
      } else if (key != null) {
        devices.value.push({ ...d, device_id: key, id: key })
      }
      overallStats.value = {
        total_devices: devices.value.length,
        compliant_devices: devices.value.filter((dev) => (dev.score != null ? dev.score : dev.compliance_score) >= 90).length,
        categories: d.categories || {},
      }
      overallScore.value = d.score || 0
    }
    // 检查完成：重新拉取当前页（后端 status 实时算分，含最新结果，且保持分页）
    await fetchStatus()
  } catch (err) {
    console.error('Compliance check error:', err)
  } finally {
    checking.value = false
  }
}

function aggregateCategories(devices) {
  const sums = { identity_auth: { label: '身份鉴别', total: 0, count: 0 }, access_control: { label: '访问控制', total: 0, count: 0 }, security_audit: { label: '安全审计', total: 0, count: 0 }, intrusion_prevention: { label: '入侵防范', total: 0, count: 0 }, data_confidentiality: { label: '数据保密性', total: 0, count: 0 } }
  devices.forEach((dev) => {
    const cats = dev.categories || {}
    Object.keys(cats).forEach((key) => {
      if (sums[key]) {
        sums[key].total += cats[key].score || 0
        sums[key].count += 1
      }
    })
  })
  const out = {}
  Object.keys(sums).forEach((key) => {
    out[key] = sums[key].count > 0 ? Math.round(sums[key].total / sums[key].count) : 0
  })
  return out
}

function handleResize() {
  radarChart?.resize()
}

onMounted(async () => {
  await fetchStatus()
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  radarChart?.dispose()
})
</script>

<style scoped>
.custom-scrollbar::-webkit-scrollbar { width: 4px; }
.custom-scrollbar::-webkit-scrollbar-track { background: var(--line-strong); border-radius: 2px; }
.custom-scrollbar::-webkit-scrollbar-thumb { background: var(--ink-faint); border-radius: 2px; }
.custom-scrollbar::-webkit-scrollbar-thumb:hover { background: #6b7280; }
</style>
