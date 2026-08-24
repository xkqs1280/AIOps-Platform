<template>
  <div class="min-h-screen bg-gray-950 text-gray-100 p-4 flex flex-col gap-4">
    <div class="flex items-center justify-between px-2">
      <div class="flex items-center gap-3">
        <div class="w-2 h-8 bg-gradient-to-b from-red-400 to-orange-600 rounded-full"></div>
        <h1 class="text-2xl font-bold tracking-wide">H3C 设备巡检</h1>
      </div>
      <button
        @click="openCreateModal"
        class="px-4 py-2 text-sm rounded-lg bg-orange-500/15 text-orange-400 border border-orange-500/30 hover:bg-orange-500/25 transition-colors"
      >
        新建巡检任务
      </button>
    </div>

    <!-- Task List -->
    <div class="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
      <div class="px-5 py-4 border-b border-gray-800 flex items-center justify-between">
        <h2 class="text-sm font-semibold text-gray-200">巡检任务</h2>
        <button @click="fetchTasks" class="text-xs text-cyan-400 hover:text-cyan-300">刷新</button>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-sm text-left">
          <thead class="bg-gray-800/50 text-gray-400 text-xs uppercase">
            <tr>
              <th class="px-5 py-3">任务名称</th>
              <th class="px-5 py-3">状态</th>
              <th class="px-5 py-3">设备数</th>
              <th class="px-5 py-3">成功 / 失败</th>
              <th class="px-5 py-3">创建时间</th>
              <th class="px-5 py-3">完成时间</th>
              <th class="px-5 py-3 text-right">操作</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-800">
            <tr v-for="task in tasks" :key="task.id" class="hover:bg-gray-800/30">
              <td class="px-5 py-3 font-medium text-gray-200">{{ task.name }}</td>
              <td class="px-5 py-3">
                <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium" :class="statusClass(task.status)">
                  {{ statusText(task.status) }}
                </span>
              </td>
              <td class="px-5 py-3 text-gray-300">{{ task.total_devices }}</td>
              <td class="px-5 py-3">
                <span class="text-green-400">{{ task.success_count }}</span>
                <span class="text-gray-500 mx-1">/</span>
                <span class="text-red-400">{{ task.failed_count }}</span>
              </td>
              <td class="px-5 py-3 text-gray-400 text-xs">{{ formatTime(task.created_at) }}</td>
              <td class="px-5 py-3 text-gray-400 text-xs">{{ formatTime(task.completed_at) }}</td>
              <td class="px-5 py-3 text-right space-x-2">
                <button
                  @click="viewDetail(task)"
                  class="px-2.5 py-1 text-xs rounded bg-cyan-500/10 text-cyan-400 border border-cyan-500/20 hover:bg-cyan-500/20"
                >详情</button>
                <button
                  v-if="task.status === 'completed'"
                  @click="downloadFile(task.id, 'excel')"
                  class="px-2.5 py-1 text-xs rounded bg-green-500/10 text-green-400 border border-green-500/20 hover:bg-green-500/20"
                >Excel</button>
                <button
                  v-if="task.status === 'completed'"
                  @click="downloadFile(task.id, 'word')"
                  class="px-2.5 py-1 text-xs rounded bg-blue-500/10 text-blue-400 border border-blue-500/20 hover:bg-blue-500/20"
                >Word</button>
                <button
                  @click="deleteTask(task)"
                  class="px-2.5 py-1 text-xs rounded bg-red-500/10 text-red-400 border border-red-500/20 hover:bg-red-500/20"
                >删除</button>
              </td>
            </tr>
            <tr v-if="!tasks.length">
              <td colspan="7" class="px-5 py-10 text-center text-gray-500 text-sm">暂无巡检任务</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Create Modal -->
    <div v-if="showCreate" class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" @click.self="closeCreate">
      <div class="bg-gray-900 border border-gray-700 rounded-xl w-full max-w-lg p-6 shadow-2xl">
        <h3 class="text-lg font-semibold mb-4">新建 H3C 巡检任务</h3>
        <div class="space-y-4">
          <div>
            <label class="text-xs text-gray-400 mb-1 block">任务名称</label>
            <input
              v-model="createForm.name"
              type="text"
              placeholder="例如：月度 H3C 交换机巡检"
              class="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:border-orange-500 focus:outline-none"
            />
          </div>
          <div>
            <label class="text-xs text-gray-400 mb-1 block">选择设备</label>
            <div class="bg-gray-800 border border-gray-700 rounded-lg p-3 max-h-60 overflow-y-auto custom-scrollbar">
              <div v-if="!h3cDevices.length" class="text-xs text-gray-500 text-center py-2">暂无 H3C 设备</div>
              <label
                v-if="h3cDevices.length"
                class="flex items-center gap-3 px-2 py-2 rounded hover:bg-gray-700/50 cursor-pointer border-b border-gray-700/60"
              >
                <input
                  type="checkbox"
                  :checked="allSelected"
                  v-indeterminate="someSelected"
                  @change="toggleSelectAll"
                  class="w-4 h-4 rounded border-gray-600 bg-gray-700 text-orange-500 focus:ring-orange-500"
                />
                <div class="flex-1">
                  <div class="text-sm text-orange-300 font-semibold">全选（共 {{ h3cDevices.length }} 台）</div>
                </div>
              </label>
              <label
                v-for="device in h3cDevices"
                :key="device.id"
                class="flex items-center gap-3 px-2 py-2 rounded hover:bg-gray-700/50 cursor-pointer"
              >
                <input
                  v-model="createForm.deviceIds"
                  :value="device.id"
                  type="checkbox"
                  class="w-4 h-4 rounded border-gray-600 bg-gray-700 text-orange-500 focus:ring-orange-500"
                />
                <div class="flex-1">
                  <div class="text-sm text-gray-200">{{ device.name }}</div>
                  <div class="text-xs text-gray-500">{{ device.ip }} · {{ device.model || '未知型号' }}</div>
                </div>
              </label>
            </div>
            <p class="text-xs text-gray-500 mt-1">已选 {{ createForm.deviceIds.length }} 台设备</p>
          </div>
        </div>
        <p v-if="createError" class="text-xs text-red-400 mt-2">{{ createError }}</p>
        <div class="flex justify-end gap-2 mt-5">
          <button @click="closeCreate" :disabled="creating" class="px-4 py-2 text-sm rounded-lg bg-gray-800 text-gray-400 hover:text-gray-200 transition-colors disabled:opacity-50">取消</button>
          <button
            @click="doCreate"
            :disabled="creating || !createForm.name || !createForm.deviceIds.length"
            class="px-4 py-2 text-sm rounded-lg bg-orange-500/20 text-orange-400 border border-orange-500/30 hover:bg-orange-500/30 transition-colors disabled:opacity-50"
          >
            {{ creating ? '创建中...' : '创建并执行' }}
          </button>
        </div>
      </div>
    </div>

    <!-- Detail Modal -->
    <div v-if="showDetail" class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" @click.self="closeDetail">
      <div class="bg-gray-900 border border-gray-700 rounded-xl w-full max-w-2xl p-6 shadow-2xl max-h-[80vh] overflow-y-auto custom-scrollbar">
        <h3 class="text-lg font-semibold mb-4">{{ currentTask?.name }} - 设备明细</h3>
        <div class="space-y-2">
          <div
            v-for="r in currentTask?.device_results || []"
            :key="r.id"
            class="bg-gray-800/50 rounded-lg p-3 flex items-center justify-between"
          >
            <div>
              <div class="text-sm text-gray-200">{{ r.device_name }} <span class="text-xs text-gray-500">({{ r.device_ip }})</span></div>
              <div v-if="r.error_message" class="text-xs text-red-400 mt-1">{{ r.error_message }}</div>
            </div>
            <div class="flex items-center gap-3">
              <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium" :class="statusClass(r.status)">
                {{ statusText(r.status) }}
              </span>
              <button
                v-if="canRetry(r)"
                @click="retryDevice(r)"
                class="px-2.5 py-1 text-xs rounded bg-orange-500/10 text-orange-400 border border-orange-500/20 hover:bg-orange-500/20 transition-colors"
                :disabled="r.status === 'running' && retryingId === r.id"
              >
                {{ retryingId === r.id ? '执行中…' : '重新执行' }}
              </button>
            </div>
          </div>
        </div>
        <div class="flex justify-end mt-5">
          <button @click="closeDetail" class="px-4 py-2 text-sm rounded-lg bg-gray-800 text-gray-400 hover:text-gray-200 transition-colors">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { getDevices, getInspectionTasks, createInspectionTask } from '../api'

const API_BASE = '/api/v1'

const tasks = ref([])
const h3cDevices = ref([])
const showCreate = ref(false)
const creating = ref(false)
const createError = ref('')
const showDetail = ref(false)
const currentTask = ref(null)
const refreshTimer = ref(null)

const createForm = ref({
  name: '',
  deviceIds: [],
})

function statusClass(status) {
  const map = {
    pending: 'bg-gray-500/10 text-gray-400 border border-gray-500/20',
    running: 'bg-blue-500/10 text-blue-400 border border-blue-500/20',
    completed: 'bg-green-500/10 text-green-400 border border-green-500/20',
    success: 'bg-green-500/10 text-green-400 border border-green-500/20',
    failed: 'bg-red-500/10 text-red-400 border border-red-500/20',
  }
  return map[status] || map.pending
}

// --- 设备列表「全选」逻辑 ---
const allSelected = computed(
  () => h3cDevices.value.length > 0 && createForm.value.deviceIds.length === h3cDevices.value.length
)
const someSelected = computed(
  () => createForm.value.deviceIds.length > 0 && !allSelected.value
)
function toggleSelectAll(e) {
  if (e.target.checked) {
    createForm.value.deviceIds = h3cDevices.value.map((d) => d.id)
  } else {
    createForm.value.deviceIds = []
  }
}
// 局部指令：让 checkbox 反映半选(indeterminate)状态
const vIndeterminate = {
  mounted: (el, binding) => { el.indeterminate = binding.value },
  updated: (el, binding) => { el.indeterminate = binding.value },
}

function statusText(status) {
  const map = {
    pending: '待执行',
    running: '执行中',
    completed: '已完成',
    success: '成功',
    failed: '失败',
  }
  return map[status] || status
}

function formatTime(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

async function fetchTasks() {
  try {
    const res = await getInspectionTasks()
    tasks.value = res.data?.items || res.items || []
  } catch (err) {
    console.error('Fetch inspection tasks error:', err)
  }
}

async function fetchH3cDevices() {
  try {
    // 分页拉取全部设备（平台上限 300 台），确保选择列表不遗漏
    const all = []
    let page = 1
    const pageSize = 100
    while (true) {
      const res = await getDevices({ page, page_size: pageSize })
      const items = res.data?.items || res.items || []
      all.push(...items)
      const total = res.data?.total ?? res.total ?? 0
      if (items.length === 0 || all.length >= total || all.length >= 300) break
      page++
    }
    h3cDevices.value = all.filter(d => {
      const v = (d.vendor || '').toLowerCase()
      return v.includes('h3c') || v.includes('华三')
    })
  } catch (err) {
    console.error('Fetch H3C devices error:', err)
  }
}

function openCreateModal() {
  createForm.value = { name: '', deviceIds: [] }
  createError.value = ''
  showCreate.value = true
  fetchH3cDevices()
}

function closeCreate() {
  showCreate.value = false
}

async function doCreate() {
  creating.value = true
  createError.value = ''
  try {
    await createInspectionTask({
      name: createForm.value.name,
      device_ids: createForm.value.deviceIds,
    })
    closeCreate()
    await fetchTasks()
    startRefresh()
  } catch (err) {
    createError.value = err.response?.data?.detail || err.message || '创建失败'
    console.error('Create inspection task error:', err)
  } finally {
    creating.value = false
  }
}

async function viewDetail(task) {
  try {
    const res = await fetch(`${API_BASE}/inspections/${task.id}`)
    const data = await res.json()
    currentTask.value = data.data || data
    showDetail.value = true
  } catch (err) {
    console.error('Fetch task detail error:', err)
  }
}

// 单台设备重新执行：failed/pending 直接可重跑；running 需超 10 分钟（卡死）
const retryingId = ref(null)
function canRetry(r) {
  if (!r || r.status === 'success') return false
  if (r.status !== 'running') return true
  if (!r.created_at) return false
  const created = new Date(r.created_at)
  if (isNaN(created.getTime())) return false
  return Date.now() - created.getTime() >= 10 * 60 * 1000
}

async function retryDevice(r) {
  if (!currentTask.value) return
  if (!confirm(`重新执行设备「${r.device_name}」的巡检？将重新采集该设备信息。`)) return
  retryingId.value = r.id
  try {
    const res = await fetch(
      `${API_BASE}/inspections/${currentTask.value.id}/devices/${r.device_id}/retry`,
      { method: 'POST' }
    )
    if (!res.ok) {
      const err = await res.json()
      alert('重新执行失败: ' + (err.detail || `HTTP ${res.status}`))
      return
    }
    await viewDetail(currentTask.value)
  } catch (err) {
    alert('重新执行失败: ' + err.message)
  } finally {
    retryingId.value = null
  }
}

function closeDetail() {
  showDetail.value = false
  currentTask.value = null
}

async function downloadFile(taskId, type) {
  try {
    const res = await fetch(`${API_BASE}/inspections/${taskId}/download/${type}`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const ext = type === 'excel' ? 'xlsx' : 'docx'
    const filename = `H3C巡检任务${taskId}.${ext}`
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  } catch (err) {
    console.error('Download error:', err)
    alert('下载失败')
  }
}

async function deleteTask(task) {
  const runningRecent = task.status === 'running'
  const msg = runningRecent
    ? `该巡检任务执行中。若已超过 1 小时视为卡死，可删除。确认删除？`
    : `确认删除巡检记录「${task.name}」？删除后不可恢复。`
  if (!confirm(msg)) return
  try {
    const res = await fetch(`${API_BASE}/inspections/${task.id}`, { method: 'DELETE' })
    if (!res.ok) {
      const err = await res.json()
      alert('删除失败: ' + (err.detail || `HTTP ${res.status}`))
      return
    }
    await fetchTasks()
  } catch (err) {
    console.error('Delete error:', err)
    alert('删除失败: ' + err.message)
  }
}

function startRefresh() {
  stopRefresh()
  refreshTimer.value = setInterval(() => {
    const hasRunning = tasks.value.some(t => t.status === 'pending' || t.status === 'running')
    if (hasRunning) fetchTasks()
  }, 3000)
}

function stopRefresh() {
  if (refreshTimer.value) {
    clearInterval(refreshTimer.value)
    refreshTimer.value = null
  }
}

onMounted(() => {
  fetchTasks()
  fetchH3cDevices()
  startRefresh()
})

onUnmounted(() => {
  stopRefresh()
})
</script>

<style scoped>
.custom-scrollbar::-webkit-scrollbar { width: 4px; }
.custom-scrollbar::-webkit-scrollbar-track { background: #1f2937; border-radius: 2px; }
.custom-scrollbar::-webkit-scrollbar-thumb { background: #4b5563; border-radius: 2px; }
.custom-scrollbar::-webkit-scrollbar-thumb:hover { background: #6b7280; }
</style>
