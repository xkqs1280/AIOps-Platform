<template>
  <div class="min-h-screen bg-gray-950 text-gray-100 p-4 flex flex-col gap-4">
    <!-- Header -->
    <div class="flex items-center justify-between px-2">
      <div class="flex items-center gap-3">
        <div class="w-2 h-8 bg-gradient-to-b from-purple-400 to-indigo-600 rounded-full"></div>
        <h1 class="text-2xl font-bold tracking-wide">重要业务监控</h1>
      </div>
      <div class="flex items-center gap-2">
        <button
          @click="probeAll"
          :disabled="probingAll"
          class="px-4 py-2 text-sm rounded-lg bg-purple-500/15 text-purple-400 border border-purple-500/30 hover:bg-purple-500/25 transition-colors disabled:opacity-50"
        >
          {{ probingAll ? '探测中...' : '立即探测全部' }}
        </button>
        <button
          @click="openGroupModal()"
          class="px-4 py-2 text-sm rounded-lg bg-gray-700/40 text-gray-300 border border-gray-600/50 hover:bg-gray-700/60 transition-colors"
        >+ 分组</button>
        <button
          @click="openTerminalModal()"
          class="px-4 py-2 text-sm rounded-lg bg-cyan-500/15 text-cyan-400 border border-cyan-500/30 hover:bg-cyan-500/25 transition-colors"
        >+ 终端</button>
      </div>
    </div>

    <!-- Stats -->
    <div class="grid grid-cols-4 gap-4">
      <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
        <div class="text-xs text-gray-500 mb-1">监控终端</div>
        <div class="text-2xl font-bold">{{ summary.total || 0 }}</div>
      </div>
      <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
        <div class="text-xs text-gray-500 mb-1">在线</div>
        <div class="text-2xl font-bold text-green-400">{{ summary.online || 0 }}</div>
      </div>
      <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
        <div class="text-xs text-gray-500 mb-1">离线</div>
        <div class="text-2xl font-bold text-red-400">{{ summary.offline || 0 }}</div>
      </div>
      <div class="bg-gray-900 border border-gray-800 rounded-xl p-4">
        <div class="text-xs text-gray-500 mb-1">离线告警次数</div>
        <div class="text-2xl font-bold text-orange-400">{{ summary.offline_alerts || 0 }}</div>
      </div>
    </div>

    <!-- Tabs -->
    <div class="flex items-center gap-2 px-2">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        @click="activeTab = tab.key"
        class="px-3 py-1.5 text-sm rounded-lg transition-colors"
        :class="activeTab === tab.key ? 'bg-gray-800 text-cyan-400' : 'text-gray-500 hover:text-gray-300'"
      >{{ tab.label }}</button>
    </div>

    <!-- Terminal List -->
    <div v-if="activeTab === 'terminals'" class="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
      <div class="flex items-center justify-between px-4 py-3 border-b border-gray-800">
        <div class="flex items-center gap-3">
          <select v-model="filterGroupId" class="bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 focus:outline-none" @change="termPage = 1; loadTerminals()">
            <option :value="null">全部分组</option>
            <option v-for="g in groups" :key="g.id" :value="g.id">{{ g.name }} ({{ g.terminal_count }})</option>
          </select>
          <select v-model="filterStatus" class="bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 focus:outline-none" @change="termPage = 1; loadTerminals()">
            <option value="">全部状态</option>
            <option value="online">在线</option>
            <option value="offline">离线</option>
            <option value="unknown">未知</option>
          </select>
          <input
            v-model="filterKeyword"
            placeholder="搜索名称/IP/描述..."
            class="bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-gray-200 focus:border-cyan-500 focus:outline-none w-56"
            @input="onTermSearch"
          />
        </div>
        <button @click="loadAll" class="text-xs text-cyan-400 hover:text-cyan-300">刷新</button>
      </div>
      <table class="w-full text-sm">
        <thead>
          <tr class="text-left text-gray-500 border-b border-gray-800">
            <th class="px-4 py-2.5 font-medium">终端名称</th>
            <th class="px-4 py-2.5 font-medium">分组</th>
            <th class="px-4 py-2.5 font-medium">IP</th>
            <th class="px-4 py-2.5 font-medium">MAC</th>
            <th class="px-4 py-2.5 font-medium">描述</th>
            <th class="px-4 py-2.5 font-medium">状态</th>
            <th class="px-4 py-2.5 font-medium">最后在线</th>
            <th class="px-4 py-2.5 font-medium text-right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in terminals" :key="t.id" class="border-b border-gray-800/60 hover:bg-gray-800/30">
            <td class="px-4 py-2.5 text-gray-200">{{ t.name }}</td>
            <td class="px-4 py-2.5 text-gray-400 text-xs">{{ groupName(t.group_id) }}</td>
            <td class="px-4 py-2.5 text-gray-300 font-mono text-xs">{{ t.ip }}</td>
            <td class="px-4 py-2.5 text-gray-500 font-mono text-xs">{{ t.mac || '-' }}</td>
            <td class="px-4 py-2.5 text-gray-500 text-xs max-w-[160px] truncate">{{ t.description || '-' }}</td>
            <td class="px-4 py-2.5">
              <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium" :class="statusClass(t.status)">
                <span class="w-1.5 h-1.5 rounded-full mr-1.5" :class="statusDot(t.status)"></span>
                {{ statusText(t.status) }}
              </span>
            </td>
            <td class="px-4 py-2.5 text-gray-400 text-xs">{{ formatTime(t.last_online_at) }}</td>
            <td class="px-4 py-2.5 text-right space-x-1.5">
              <button
                @click="probeOne(t)"
                :disabled="probingId === t.id"
                class="px-2 py-0.5 text-xs rounded bg-purple-500/10 text-purple-400 border border-purple-500/20 hover:bg-purple-500/20 disabled:opacity-50"
              >{{ probingId === t.id ? '探测中' : '探测' }}</button>
              <button @click="openTerminalModal(t)" class="px-2 py-0.5 text-xs rounded bg-gray-700/40 text-gray-300 border border-gray-600/40 hover:bg-gray-700/60">编辑</button>
              <button @click="deleteTerminal(t)" class="px-2 py-0.5 text-xs rounded bg-red-500/10 text-red-400 border border-red-500/20 hover:bg-red-500/20">删除</button>
            </td>
          </tr>
          <tr v-if="terminals.length === 0">
            <td colspan="8" class="px-4 py-10 text-center text-gray-500 text-sm">暂无终端，点击右上角"添加终端"开始监控</td>
          </tr>
        </tbody>
      </table>

      <!-- 终端分页 -->
      <div v-if="termTotal > 0" class="flex items-center justify-between px-4 py-3 border-t border-gray-800 text-sm text-gray-500">
        <span>共 {{ termTotal }} 台终端</span>
        <div class="flex items-center gap-3">
          <select
            v-model="termPageSize"
            class="px-2 py-1 bg-gray-800 border border-gray-700 rounded text-gray-300 focus:outline-none focus:border-cyan-500"
            @change="termPage = 1; loadTerminals()"
          >
            <option :value="10">10 条/页</option>
            <option :value="20">20 条/页</option>
            <option :value="50">50 条/页</option>
          </select>
          <button
            class="px-3 py-1 bg-gray-800 border border-gray-700 rounded text-gray-300 hover:bg-gray-700 disabled:opacity-40"
            :disabled="termPage <= 1"
            @click="termPage--; loadTerminals()"
          >上一页</button>
          <span>第 {{ termPage }} / {{ termTotalPages }} 页</span>
          <button
            class="px-3 py-1 bg-gray-800 border border-gray-700 rounded text-gray-300 hover:bg-gray-700 disabled:opacity-40"
            :disabled="termPage >= termTotalPages"
            @click="termPage++; loadTerminals()"
          >下一页</button>
        </div>
      </div>
    </div>

    <!-- Alert Records -->
    <div v-if="activeTab === 'alerts'" class="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
      <div class="flex items-center justify-between px-4 py-3 border-b border-gray-800">
        <h3 class="text-sm text-gray-400">离线/恢复告警记录</h3>
        <div class="flex items-center gap-3">
          <button @click="loadAlerts" class="text-xs text-cyan-400 hover:text-cyan-300">刷新</button>
          <button
            @click="clearAlerts"
            :disabled="alerts.length === 0"
            class="text-xs text-red-400 hover:text-red-300 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            一键清空
          </button>
        </div>
      </div>
      <table class="w-full text-sm">
        <thead>
          <tr class="text-left text-gray-500 border-b border-gray-800">
            <th class="px-4 py-2.5 font-medium">时间</th>
            <th class="px-4 py-2.5 font-medium">类型</th>
            <th class="px-4 py-2.5 font-medium">终端</th>
            <th class="px-4 py-2.5 font-medium">内容</th>
            <th class="px-4 py-2.5 font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in alerts" :key="a.id" class="border-b border-gray-800/60">
            <td class="px-4 py-2.5 text-gray-400 text-xs">{{ formatTime(a.created_at) }}</td>
            <td class="px-4 py-2.5">
              <span class="px-2 py-0.5 rounded text-xs font-medium" :class="a.alert_type === 'offline' ? 'bg-red-500/15 text-red-400' : 'bg-green-500/15 text-green-400'">
                {{ a.alert_type === 'offline' ? '离线告警' : '已恢复' }}
              </span>
            </td>
            <td class="px-4 py-2.5 text-gray-200">{{ a.terminal_name }} <span class="text-gray-500 font-mono text-xs">({{ a.terminal_ip }})</span></td>
            <td class="px-4 py-2.5 text-gray-400 text-xs">{{ a.message }}</td>
            <td class="px-4 py-2.5">
              <button
                @click="removeAlert(a)"
                class="px-2 py-1 text-xs rounded bg-red-500/10 text-red-400 border border-red-500/20 hover:bg-red-500/20 transition-colors"
              >
                删除
              </button>
            </td>
          </tr>
          <tr v-if="alerts.length === 0">
            <td colspan="5" class="px-4 py-10 text-center text-gray-500 text-sm">暂无告警记录</td>
          </tr>
        </tbody>
      </table>

      <!-- 分页 -->
      <div v-if="alertTotal > 0" class="flex items-center justify-between px-4 py-3 border-t border-gray-800 text-sm text-gray-500">
        <span>共 {{ alertTotal }} 条记录</span>
        <div class="flex items-center gap-3">
          <select
            v-model="alertPageSize"
            class="px-2 py-1 bg-gray-800 border border-gray-700 rounded text-gray-300 focus:outline-none focus:border-cyan-500"
            @change="alertPage = 1; loadAlerts()"
          >
            <option :value="10">10 条/页</option>
            <option :value="20">20 条/页</option>
            <option :value="50">50 条/页</option>
          </select>
          <button
            class="px-3 py-1 bg-gray-800 border border-gray-700 rounded text-gray-300 hover:bg-gray-700 disabled:opacity-40"
            :disabled="alertPage <= 1"
            @click="alertPage--; loadAlerts()"
          >上一页</button>
          <span>第 {{ alertPage }} / {{ alertTotalPages }} 页</span>
          <button
            class="px-3 py-1 bg-gray-800 border border-gray-700 rounded text-gray-300 hover:bg-gray-700 disabled:opacity-40"
            :disabled="alertPage >= alertTotalPages"
            @click="alertPage++; loadAlerts()"
          >下一页</button>
        </div>
      </div>
    </div>

    <!-- Group Modal -->
    <div v-if="showGroupModal" class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" @click.self="showGroupModal = false">
      <div class="bg-gray-900 border border-gray-700 rounded-xl w-full max-w-sm p-6 shadow-2xl">
        <h3 class="text-lg font-semibold mb-4">{{ groupForm.id ? '编辑分组' : '添加分组' }}</h3>
        <div class="space-y-3">
          <div>
            <label class="text-xs text-gray-400 mb-1 block">分组名称</label>
            <input v-model="groupForm.name" class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:border-cyan-500 focus:outline-none" placeholder="如: 厂区监控" />
          </div>
          <div>
            <label class="text-xs text-gray-400 mb-1 block">描述</label>
            <input v-model="groupForm.description" class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:border-cyan-500 focus:outline-none" placeholder="可选" />
          </div>
        </div>
        <div class="flex justify-end gap-2 mt-5">
          <button @click="showGroupModal = false" class="px-4 py-2 text-sm rounded-lg bg-gray-800 text-gray-400 hover:text-gray-200 transition-colors">取消</button>
          <button @click="submitGroup" class="px-4 py-2 text-sm rounded-lg bg-cyan-500/20 text-cyan-400 border border-cyan-500/30 hover:bg-cyan-500/30 transition-colors">保存</button>
        </div>
      </div>
    </div>

    <!-- Terminal Modal -->
    <div v-if="showTerminalModal" class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" @click.self="showTerminalModal = false">
      <div class="bg-gray-900 border border-gray-700 rounded-xl w-full max-w-md p-6 shadow-2xl">
        <h3 class="text-lg font-semibold mb-4">{{ termForm.id ? '编辑终端' : '添加监控终端' }}</h3>

        <!-- 模式切换（编辑时固定单个） -->
        <div v-if="!termForm.id" class="flex gap-2 mb-4">
          <button
            class="px-3 py-1.5 rounded-lg text-sm font-medium transition-colors"
            :class="termMode === 'single' ? 'bg-cyan-500/20 text-cyan-400 border border-cyan-500/40' : 'bg-gray-800 text-gray-400 hover:bg-gray-700'"
            @click="termMode = 'single'"
          >单个添加</button>
          <button
            class="px-3 py-1.5 rounded-lg text-sm font-medium transition-colors"
            :class="termMode === 'batch' ? 'bg-cyan-500/20 text-cyan-400 border border-cyan-500/40' : 'bg-gray-800 text-gray-400 hover:bg-gray-700'"
            @click="termMode = 'batch'"
          >批量添加</button>
        </div>

        <div v-if="termMode === 'single' || termForm.id" class="space-y-3">
          <div>
            <label class="text-xs text-gray-400 mb-1 block">所属分组</label>
            <select v-model="termForm.group_id" class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none">
              <option v-for="g in groups" :key="g.id" :value="g.id">{{ g.name }}</option>
            </select>
          </div>
          <div>
            <label class="text-xs text-gray-400 mb-1 block">终端名称</label>
            <input v-model="termForm.name" class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:border-cyan-500 focus:outline-none" placeholder="如: 大厅东监控" />
          </div>
          <div>
            <label class="text-xs text-gray-400 mb-1 block">IP 地址</label>
            <input v-model="termForm.ip" class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:border-cyan-500 focus:outline-none font-mono" placeholder="如: 192.168.1.50" />
          </div>
          <div>
            <label class="text-xs text-gray-400 mb-1 block">MAC 地址（可选）</label>
            <input v-model="termForm.mac" class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:border-cyan-500 focus:outline-none font-mono" placeholder="如: 00-11-22-33-44-55" />
          </div>
          <div>
            <label class="text-xs text-gray-400 mb-1 block">描述</label>
            <input v-model="termForm.description" class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:border-cyan-500 focus:outline-none" placeholder="如: 一楼大厅东侧" />
          </div>
          <label class="flex items-center gap-2 text-sm text-gray-300">
            <input type="checkbox" v-model="termForm.enabled" class="accent-cyan-500" /> 启用监控
          </label>
        </div>

        <!-- 批量添加表单 -->
        <div v-else class="space-y-3">
          <div>
            <label class="text-xs text-gray-400 mb-1 block">所属分组</label>
            <select v-model="batchForm.group_id" class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none">
              <option v-for="g in groups" :key="g.id" :value="g.id">{{ g.name }}</option>
            </select>
          </div>
          <div>
            <label class="text-xs text-gray-400 mb-1 block">名称前缀</label>
            <input v-model="batchForm.name_prefix" class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:border-cyan-500 focus:outline-none" placeholder="如: AP" />
            <p class="text-[11px] text-gray-500 mt-1">生成的名称：{{ batchForm.name_prefix }}_1、{{ batchForm.name_prefix }}_2 …</p>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="text-xs text-gray-400 mb-1 block">起始序号</label>
              <input v-model.number="batchForm.start_index" type="number" min="1" class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:border-cyan-500 focus:outline-none" />
            </div>
            <div>
              <label class="text-xs text-gray-400 mb-1 block">数量</label>
              <input v-model.number="batchForm.count" type="number" min="1" max="200" class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:border-cyan-500 focus:outline-none" />
            </div>
          </div>
          <div>
            <label class="text-xs text-gray-400 mb-1 block">起始 IP</label>
            <input v-model="batchForm.start_ip" class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:border-cyan-500 focus:outline-none font-mono" placeholder="如: 192.168.1.50" />
            <p class="text-[11px] text-gray-500 mt-1">IP 从 {{ batchForm.start_ip || '起始IP' }} 开始逐台 +1 递增</p>
          </div>
          <div>
            <label class="text-xs text-gray-400 mb-1 block">描述（可选，统一备注）</label>
            <input v-model="batchForm.description" class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:border-cyan-500 focus:outline-none" placeholder="如: 一楼 AP" />
          </div>
        </div>

        <div class="flex justify-end gap-2 mt-5">
          <button @click="showTerminalModal = false" class="px-4 py-2 text-sm rounded-lg bg-gray-800 text-gray-400 hover:text-gray-200 transition-colors">取消</button>
          <button
            @click="termMode === 'batch' && !termForm.id ? submitBatchTerminals() : submitTerminal()"
            class="px-4 py-2 text-sm rounded-lg bg-cyan-500/20 text-cyan-400 border border-cyan-500/30 hover:bg-cyan-500/30 transition-colors"
          >保存</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'

const API_BASE = '/api/v1'
const activeTab = ref('terminals')
const tabs = [
  { key: 'terminals', label: '终端状态' },
  { key: 'alerts', label: '告警记录' },
]

const groups = ref([])
const terminals = ref([])
const alerts = ref([])
const summary = ref({})
const filterGroupId = ref(null)
const filterStatus = ref('')
const filterKeyword = ref('')
const probingAll = ref(false)
const probingId = ref(null)
// 终端列表分页
const termPage = ref(1)
const termPageSize = ref(10)
const termTotal = ref(0)
const termTotalPages = computed(() => Math.max(1, Math.ceil(termTotal.value / termPageSize.value)))
// 告警记录分页
const alertPage = ref(1)
const alertPageSize = ref(10)
const alertTotal = ref(0)
const alertTotalPages = computed(() => Math.max(1, Math.ceil(alertTotal.value / alertPageSize.value)))

// 弹窗
const showGroupModal = ref(false)
const groupForm = ref({ id: null, name: '', description: '' })
const showTerminalModal = ref(false)
const termForm = ref({ id: null, group_id: null, name: '', ip: '', mac: '', description: '', enabled: true })
// 终端添加模式：单个 / 批量
const termMode = ref('single')
const batchForm = ref({ group_id: null, name_prefix: '', start_index: 1, start_ip: '', count: 50, description: '' })

// 搜索防抖
let termSearchTimer = null
function onTermSearch() {
  if (termSearchTimer) clearTimeout(termSearchTimer)
  termSearchTimer = setTimeout(() => {
    termPage.value = 1
    loadTerminals()
  }, 300)
}

function groupName(gid) {
  const g = groups.value.find(x => x.id === gid)
  return g ? g.name : '-'
}

function statusText(s) {
  return { online: '在线', offline: '离线', unknown: '未知' }[s] || s
}
function statusClass(s) {
  return { online: 'bg-green-500/15 text-green-400', offline: 'bg-red-500/15 text-red-400', unknown: 'bg-gray-500/15 text-gray-400' }[s] || 'bg-gray-500/15 text-gray-400'
}
function statusDot(s) {
  return { online: 'bg-green-500', offline: 'bg-red-500', unknown: 'bg-gray-500' }[s] || 'bg-gray-500'
}

function formatTime(v) {
  if (!v) return '-'
  const d = new Date(v)
  if (isNaN(d.getTime())) return v
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

async function loadAll() {
  await Promise.all([loadGroups(), loadTerminals(), loadSummary()])
}
async function loadGroups() {
  try {
    const res = await fetch(`${API_BASE}/business-monitor/groups`)
    const data = await res.json()
    groups.value = data.items || []
  } catch (e) { console.error(e) }
}
async function loadTerminals() {
  try {
    let url = `${API_BASE}/business-monitor/terminals?page=${termPage.value}&page_size=${termPageSize.value}`
    if (filterGroupId.value) url += `&group_id=${filterGroupId.value}`
    if (filterStatus.value) url += `&status=${filterStatus.value}`
    const kw = filterKeyword.value.trim()
    if (kw) url += `&keyword=${encodeURIComponent(kw)}`
    const res = await fetch(url)
    const data = await res.json()
    terminals.value = data.items || []
    termTotal.value = data.total || 0
  } catch (e) { console.error(e) }
}
async function loadAlerts() {
  try {
    const res = await fetch(`${API_BASE}/business-monitor/alerts?page=${alertPage.value}&page_size=${alertPageSize.value}`)
    const data = await res.json()
    alerts.value = data.items || []
    alertTotal.value = data.total || 0
  } catch (e) { console.error(e) }
}
async function removeAlert(a) {
  if (!confirm(`确定删除该告警记录吗？\n${a.terminal_name}（${a.terminal_ip}）：${a.message || ''}`)) return
  const res = await fetch(`${API_BASE}/business-monitor/alerts/${a.id}`, { method: 'DELETE' })
  if (!res.ok && res.status !== 204) {
    alert('删除失败：HTTP ' + res.status)
    return
  }
  // 当前页删空则回退一页
  if (alerts.value.length === 1 && alertPage.value > 1) alertPage.value--
  await loadAlerts()
}
async function clearAlerts() {
  if (!alerts.value.length) return
  if (!confirm('确定清空全部业务监控告警记录吗？此操作不可恢复。')) return
  try {
    const res = await fetch(`${API_BASE}/business-monitor/alerts`, { method: 'DELETE' })
    const data = await res.json().catch(() => ({}))
    alertPage.value = 1
    await loadAlerts()
    await loadSummary()
    if (data.deleted) alert(`已清空 ${data.deleted} 条告警记录`)
  } catch (e) {
    alert('清空失败：' + e.message)
  }
}
async function loadSummary() {
  try {
    const res = await fetch(`${API_BASE}/business-monitor/summary`)
    summary.value = await res.json()
  } catch (e) { console.error(e) }
}

// 分组
function openGroupModal(g) {
  groupForm.value = g ? { id: g.id, name: g.name, description: g.description } : { id: null, name: '', description: '' }
  showGroupModal.value = true
}
async function submitGroup() {
  if (!groupForm.value.name) { alert('请输入分组名称'); return }
  const method = groupForm.value.id ? 'PUT' : 'POST'
  const url = groupForm.value.id
    ? `${API_BASE}/business-monitor/groups/${groupForm.value.id}`
    : `${API_BASE}/business-monitor/groups`
  const res = await fetch(url, {
    method, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: groupForm.value.name, description: groupForm.value.description }),
  })
  if (!res.ok) {
    const err = await res.json()
    alert('保存失败: ' + (err.detail || '未知错误'))
    return
  }
  showGroupModal.value = false
  await loadGroups()
}

// 终端
function openTerminalModal(t) {
  termForm.value = t
    ? { id: t.id, group_id: t.group_id, name: t.name, ip: t.ip, mac: t.mac || '', description: t.description || '', enabled: t.enabled }
    : { id: null, group_id: groups.value[0]?.id || null, name: '', ip: '', mac: '', description: '', enabled: true }
  termMode.value = 'single'
  batchForm.value = { group_id: groups.value[0]?.id || null, name_prefix: '', start_index: 1, start_ip: '', count: 50, description: '' }
  showTerminalModal.value = true
}
function isValidIP(ip) {
  if (!ip || typeof ip !== 'string') return false
  ip = ip.trim()
  const parts = ip.split('.')
  if (parts.length === 4 && parts.every(p => /^\d{1,3}$/.test(p) && Number(p) <= 255)) return true
  if (ip.includes(':') && ip.split(':').length >= 2) return true
  return false
}

async function submitTerminal() {
  const f = termForm.value
  if (!f.group_id) { alert('请先创建分组'); return }
  if (!f.name) { alert('请输入终端名称'); return }
  if (!f.ip) { alert('请输入 IP 地址'); return }
  if (!isValidIP(f.ip)) { alert('IP 地址格式不正确，请检查后重试'); return }
  const method = f.id ? 'PUT' : 'POST'
  const url = f.id ? `${API_BASE}/business-monitor/terminals/${f.id}` : `${API_BASE}/business-monitor/terminals`
  const res = await fetch(url, {
    method, headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ group_id: f.group_id, name: f.name, ip: f.ip, mac: f.mac || null, description: f.description || null, enabled: f.enabled }),
  })
  if (!res.ok) {
    const err = await res.json()
    alert('保存失败: ' + (err.detail || '未知错误'))
    return
  }
  showTerminalModal.value = false
  await loadAll()
}

async function submitBatchTerminals() {
  const b = batchForm.value
  if (!b.group_id) { alert('请先创建分组'); return }
  if (!b.name_prefix) { alert('请输入名称前缀'); return }
  if (!b.start_ip) { alert('请输入起始 IP 地址'); return }
  if (!isValidIP(b.start_ip)) { alert('起始 IP 地址格式不正确'); return }
  if (!b.count || b.count < 1 || b.count > 200) { alert('数量需在 1-200 之间'); return }
  if (!b.start_index || b.start_index < 1) { alert('起始序号需 ≥ 1'); return }
  const res = await fetch(`${API_BASE}/business-monitor/terminals/batch`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      group_id: b.group_id,
      name_prefix: b.name_prefix,
      start_index: b.start_index,
      start_ip: b.start_ip,
      count: b.count,
      description: b.description || null,
    }),
  })
  if (!res.ok) {
    const err = await res.json()
    alert('批量添加失败: ' + (err.detail || '未知错误'))
    return
  }
  const data = await res.json()
  showTerminalModal.value = false
  await loadAll()
  alert(`批量添加完成：成功 ${data.created} 台${data.skipped ? `，跳过已存在 ${data.skipped} 台` : ''}`)
}

async function deleteTerminal(t) {
  if (!confirm(`确认删除终端「${t.name}」？其告警记录将一并删除。`)) return
  const res = await fetch(`${API_BASE}/business-monitor/terminals/${t.id}`, { method: 'DELETE' })
  if (!res.ok) {
    const err = await res.json()
    alert('删除失败: ' + (err.detail || '未知错误'))
    return
  }
  // 当前页删空则回退一页
  if (terminals.value.length === 1 && termPage.value > 1) termPage.value--
  await loadAll()
}

// 探测
async function probeOne(t) {
  probingId.value = t.id
  try {
    const res = await fetch(`${API_BASE}/business-monitor/terminals/${t.id}/probe`, { method: 'POST' })
    await loadAll()
    if (!res.ok) alert('探测失败')
  } catch (e) { console.error(e) } finally {
    probingId.value = null
  }
}
async function probeAll() {
  probingAll.value = true
  try {
    const res = await fetch(`${API_BASE}/business-monitor/terminals/probe-all`, { method: 'POST' })
    const data = await res.json()
    await loadAll()
    alert(`探测完成，共 ${data.probed || 0} 台终端`)
  } catch (e) {
    alert('探测失败: ' + e.message)
  } finally {
    probingAll.value = false
  }
}

onMounted(() => {
  loadAll()
  loadAlerts()
})
onUnmounted(() => {
  if (termSearchTimer) clearTimeout(termSearchTimer)
})
</script>
