<template>
  <div class="min-h-screen bg-gray-950 text-gray-100 p-6">
    <!-- Header -->
    <div class="flex items-center justify-between mb-6">
      <h2 class="text-xl font-bold">配置备份</h2>
      <div class="flex gap-3">
        <button
          class="px-4 py-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm font-medium transition-colors border border-gray-700"
          @click="activeTab = 'backups'"
          :class="{ 'bg-cyan-600/20 text-cyan-400 border-cyan-600/50': activeTab === 'backups' }"
        >
          备份记录
        </button>
        <button
          class="px-4 py-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-sm font-medium transition-colors border border-gray-700"
          @click="activeTab = 'schedules'"
          :class="{ 'bg-cyan-600/20 text-cyan-400 border-cyan-600/50': activeTab === 'schedules' }"
        >
          备份计划
        </button>
      </div>
    </div>

    <!-- Tab: 备份记录 -->
    <div v-if="activeTab === 'backups'">
      <!-- Filter bar -->
      <div class="flex flex-wrap items-end gap-4 mb-6">
        <div class="w-48">
          <label class="block text-sm text-gray-400 mb-1">设备筛选</label>
          <select v-model="filterDeviceId" class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-gray-100 focus:outline-none focus:border-cyan-500" @change="loadBackups">
            <option value="">全部设备</option>
            <option v-for="d in devices" :key="d.id" :value="d.id">{{ d.name }} ({{ d.ip }})</option>
          </select>
        </div>
        <div class="w-36">
          <label class="block text-sm text-gray-400 mb-1">状态</label>
          <select v-model="filterStatus" class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-gray-100 focus:outline-none focus:border-cyan-500" @change="loadBackups">
            <option value="">全部</option>
            <option value="success">成功</option>
            <option value="failed">失败</option>
          </select>
        </div>
        <button class="px-4 py-2 bg-cyan-600 hover:bg-cyan-700 rounded-lg text-white font-medium text-sm transition-colors" @click="showBackupDialog = true">
          + 手动备份
        </button>
        <button class="px-4 py-2 bg-purple-600 hover:bg-purple-700 rounded-lg text-white font-medium text-sm transition-colors" @click="backupAll">
          备份全部设备
        </button>
        <button class="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-gray-200 font-medium text-sm transition-colors" @click="loadBackups">
          刷新
        </button>
        <button
          v-if="selectedBackups.length === 2"
          class="px-4 py-2 bg-orange-600 hover:bg-orange-700 rounded-lg text-white font-medium text-sm transition-colors"
          @click="compareSelected"
        >
          对比选中 ({{ selectedBackups.length }}/2)
        </button>
      </div>

      <!-- Backup table -->
      <div class="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
        <div class="overflow-x-auto">
          <table class="w-full">
            <thead>
              <tr class="bg-gray-800/50 text-left text-sm text-gray-400">
                <th class="px-4 py-3 font-medium w-10">
                  <input type="checkbox" class="rounded bg-gray-700 border-gray-600" :checked="selectedBackups.length === backups.length && backups.length > 0" @change="toggleSelectAll($event)">
                </th>
                <th class="px-4 py-3 font-medium">设备</th>
                <th class="px-4 py-3 font-medium">类型</th>
                <th class="px-4 py-3 font-medium">大小</th>
                <th class="px-4 py-3 font-medium">行数</th>
                <th class="px-4 py-3 font-medium">状态</th>
                <th class="px-4 py-3 font-medium">备份时间</th>
                <th class="px-4 py-3 font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="b in backups" :key="b.id" class="border-t border-gray-800 hover:bg-gray-800/30 text-sm">
                <td class="px-4 py-3">
                  <input
                    type="checkbox"
                    class="rounded bg-gray-700 border-gray-600"
                    :value="b.id"
                    :checked="selectedBackups.includes(b.id)"
                    :disabled="selectedBackups.length >= 2 && !selectedBackups.includes(b.id)"
                    @change="toggleSelect(b.id)"
                  >
                </td>
                <td class="px-4 py-3">{{ getDeviceName(b.device_id) }}</td>
                <td class="px-4 py-3">
                  <span :class="b.backup_type === 'manual' ? 'text-blue-400' : 'text-green-400'" class="text-xs px-2 py-0.5 rounded-full bg-gray-800">
                    {{ b.backup_type === 'manual' ? '手动' : '定时' }}
                  </span>
                </td>
                <td class="px-4 py-3 text-gray-400">{{ b.file_size > 0 ? (b.file_size / 1024).toFixed(1) + ' KB' : '-' }}</td>
                <td class="px-4 py-3 text-gray-400">{{ b.line_count || '-' }}</td>
                <td class="px-4 py-3">
                  <span :class="b.status === 'success' ? 'text-green-400' : 'text-red-400'" class="text-xs px-2 py-0.5 rounded-full" :style="b.status === 'success' ? 'background: rgba(34,197,94,0.15)' : 'background: rgba(239,68,68,0.15)'">
                    {{ b.status === 'success' ? '成功' : '失败' }}
                  </span>
                </td>
                <td class="px-4 py-3 text-gray-400">{{ formatTime(b.created_at) }}</td>
                <td class="px-4 py-3">
                  <div class="flex gap-2">
                    <button v-if="b.status === 'success'" class="text-cyan-400 hover:text-cyan-300 text-xs" @click="viewConfig(b.id)">查看</button>
                    <button v-if="b.status !== 'success' && b.error_message" class="text-orange-400 hover:text-orange-300 text-xs" @click="showError(b)">原因</button>
                    <button class="text-red-400 hover:text-red-300 text-xs" @click="deleteBackup(b.id)">删除</button>
                  </div>
                </td>
              </tr>
              <tr v-if="backups.length === 0">
                <td colspan="8" class="px-4 py-12 text-center text-gray-500">暂无备份记录</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 分页 -->
        <div v-if="backupTotal > 0" class="flex items-center justify-between px-4 py-3 border-t border-gray-800 text-sm text-gray-400">
          <span>共 {{ backupTotal }} 条记录</span>
          <div class="flex items-center gap-3">
            <select
              v-model="backupPageSize"
              class="px-2 py-1 bg-gray-800 border border-gray-700 rounded text-gray-300 focus:outline-none focus:border-cyan-500"
              @change="backupPage = 1; loadBackups()"
            >
              <option :value="10">10 条/页</option>
              <option :value="20">20 条/页</option>
              <option :value="50">50 条/页</option>
            </select>
            <button
              class="px-3 py-1 bg-gray-800 border border-gray-700 rounded text-gray-300 hover:bg-gray-700 disabled:opacity-40"
              :disabled="backupPage <= 1"
              @click="backupPage--; loadBackups()"
            >上一页</button>
            <span>第 {{ backupPage }} / {{ backupTotalPages }} 页</span>
            <button
              class="px-3 py-1 bg-gray-800 border border-gray-700 rounded text-gray-300 hover:bg-gray-700 disabled:opacity-40"
              :disabled="backupPage >= backupTotalPages"
              @click="backupPage++; loadBackups()"
            >下一页</button>
          </div>
        </div>
      </div>
    </div>

    <!-- Tab: 备份计划 -->
    <div v-if="activeTab === 'schedules'">
      <div class="flex items-center justify-between mb-4">
        <p class="text-sm text-gray-400">为设备设置定时备份计划，支持每日/每周/每月自动备份运行配置</p>
        <button class="px-4 py-2 bg-cyan-600 hover:bg-cyan-700 rounded-lg text-white font-medium text-sm transition-colors" @click="showScheduleDialog = true">
          + 新建计划
        </button>
      </div>

      <div class="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
        <div class="overflow-x-auto">
          <table class="w-full">
            <thead>
              <tr class="bg-gray-800/50 text-left text-sm text-gray-400">
                <th class="px-4 py-3 font-medium">设备</th>
                <th class="px-4 py-3 font-medium">频率</th>
                <th class="px-4 py-3 font-medium">执行时间</th>
                <th class="px-4 py-3 font-medium">上次备份</th>
                <th class="px-4 py-3 font-medium">下次备份</th>
                <th class="px-4 py-3 font-medium">状态</th>
                <th class="px-4 py-3 font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="s in schedules" :key="s.id" class="border-t border-gray-800 hover:bg-gray-800/30 text-sm">
                <td class="px-4 py-3">
                  <span v-if="s.is_all_devices" class="text-purple-400 font-medium">全部设备</span>
                  <span v-else>{{ getDeviceName(s.device_id) }}</span>
                </td>
                <td class="px-4 py-3">
                  <span class="text-xs px-2 py-0.5 rounded-full bg-gray-800 text-cyan-400">
                    {{ freqLabel(s.frequency) }}
                  </span>
                </td>
                <td class="px-4 py-3 text-gray-400">{{ scheduleTime(s) }}</td>
                <td class="px-4 py-3 text-gray-400">{{ s.last_backup_at ? formatTime(s.last_backup_at) : '尚未执行' }}</td>
                <td class="px-4 py-3 text-gray-400">{{ s.next_backup_at ? formatTime(s.next_backup_at) : '-' }}</td>
                <td class="px-4 py-3">
                  <span :class="s.enabled ? 'text-green-400' : 'text-gray-500'" class="text-xs">
                    {{ s.enabled ? '启用' : '停用' }}
                  </span>
                </td>
                <td class="px-4 py-3">
                  <div class="flex gap-2">
                    <button class="text-cyan-400 hover:text-cyan-300 text-xs" @click="editSchedule(s)">编辑</button>
                    <button class="text-orange-400 hover:text-orange-300 text-xs" @click="toggleSchedule(s)">{{ s.enabled ? '停用' : '启用' }}</button>
                    <button class="text-red-400 hover:text-red-300 text-xs" @click="deleteSchedule(s.id)">删除</button>
                  </div>
                </td>
              </tr>
              <tr v-if="schedules.length === 0">
                <td colspan="7" class="px-4 py-12 text-center text-gray-500">暂无备份计划</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Manual Backup Dialog -->
    <div v-if="showBackupDialog" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" @click.self="showBackupDialog = false">
      <div class="bg-gray-900 rounded-xl border border-gray-700 p-6 w-96">
        <h3 class="text-lg font-bold mb-4">手动备份</h3>
        <div class="mb-4">
          <label class="block text-sm text-gray-400 mb-2">选择设备</label>
          <select v-model="backupDeviceId" class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-gray-100 focus:outline-none focus:border-cyan-500">
            <option v-for="d in devices" :key="d.id" :value="d.id">{{ d.name }} ({{ d.ip }})</option>
          </select>
        </div>
        <div class="flex gap-3 justify-end">
          <button class="px-4 py-2 text-gray-400 hover:text-gray-200 text-sm" @click="showBackupDialog = false">取消</button>
          <button class="px-4 py-2 bg-cyan-600 hover:bg-cyan-700 rounded-lg text-white text-sm font-medium transition-colors" :disabled="!backupDeviceId || backing" @click="doManualBackup">
            {{ backing ? '备份中...' : '开始备份' }}
          </button>
        </div>
      </div>
    </div>

    <!-- Schedule Dialog -->
    <div v-if="showScheduleDialog" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" @click.self="showScheduleDialog = false">
      <div class="bg-gray-900 rounded-xl border border-gray-700 p-6 w-[420px]">
        <h3 class="text-lg font-bold mb-4">{{ editingSchedule ? '编辑备份计划' : '新建备份计划' }}</h3>
        <div class="space-y-4">
          <div>
            <label class="block text-sm text-gray-400 mb-2">备份范围</label>
            <div class="flex items-center gap-4 mb-2">
              <label class="flex items-center gap-2 cursor-pointer">
                <input type="radio" v-model="scheduleForm.is_all_devices" :value="false" class="bg-gray-800 border-gray-600">
                <span class="text-sm text-gray-300">指定设备</span>
              </label>
              <label class="flex items-center gap-2 cursor-pointer">
                <input type="radio" v-model="scheduleForm.is_all_devices" :value="true" class="bg-gray-800 border-gray-600">
                <span class="text-sm text-purple-400 font-medium">全部设备</span>
              </label>
            </div>
            <select v-if="!scheduleForm.is_all_devices" v-model="scheduleForm.device_id" :disabled="!!editingSchedule" class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-gray-100 focus:outline-none focus:border-cyan-500">
              <option v-for="d in devices" :key="d.id" :value="d.id">{{ d.name }} ({{ d.ip }})</option>
            </select>
            <p v-else class="text-xs text-purple-400/70 bg-purple-900/20 rounded-lg px-3 py-2 border border-purple-800/30">
              将自动备份当前纳管的所有设备，新增设备后也会自动包含
            </p>
          </div>
          <div>
            <label class="block text-sm text-gray-400 mb-2">备份频率</label>
            <select v-model="scheduleForm.frequency" class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-gray-100 focus:outline-none focus:border-cyan-500">
              <option value="daily">每天</option>
              <option value="weekly">每周</option>
              <option value="monthly">每月</option>
            </select>
          </div>
          <div v-if="scheduleForm.frequency === 'weekly'">
            <label class="block text-sm text-gray-400 mb-2">星期</label>
            <select v-model="scheduleForm.day_of_week" class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-gray-100 focus:outline-none focus:border-cyan-500">
              <option :value="0">周一</option>
              <option :value="1">周二</option>
              <option :value="2">周三</option>
              <option :value="3">周四</option>
              <option :value="4">周五</option>
              <option :value="5">周六</option>
              <option :value="6">周日</option>
            </select>
          </div>
          <div v-if="scheduleForm.frequency === 'monthly'">
            <label class="block text-sm text-gray-400 mb-2">日期 (1-28)</label>
            <input type="number" v-model="scheduleForm.day_of_month" min="1" max="28" class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-gray-100 focus:outline-none focus:border-cyan-500">
          </div>
          <div class="flex gap-4">
            <div class="flex-1">
              <label class="block text-sm text-gray-400 mb-2">小时 (0-23)</label>
              <input type="number" v-model="scheduleForm.hour" min="0" max="23" class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-gray-100 focus:outline-none focus:border-cyan-500">
            </div>
            <div class="flex-1">
              <label class="block text-sm text-gray-400 mb-2">分钟 (0-59)</label>
              <input type="number" v-model="scheduleForm.minute" min="0" max="59" class="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-gray-100 focus:outline-none focus:border-cyan-500">
            </div>
          </div>
        </div>
        <div class="flex gap-3 justify-end mt-6">
          <button class="px-4 py-2 text-gray-400 hover:text-gray-200 text-sm" @click="showScheduleDialog = false">取消</button>
          <button class="px-4 py-2 bg-cyan-600 hover:bg-cyan-700 rounded-lg text-white text-sm font-medium transition-colors" @click="saveSchedule">
            {{ editingSchedule ? '保存' : '创建' }}
          </button>
        </div>
      </div>
    </div>

    <!-- Config Viewer Dialog -->
    <div v-if="showConfigDialog" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" @click.self="showConfigDialog = false">
      <div class="bg-gray-900 rounded-xl border border-gray-700 w-[800px] max-h-[80vh] flex flex-col">
        <div class="flex items-center justify-between px-6 py-4 border-b border-gray-800">
          <h3 class="text-lg font-bold">配置内容 - {{ getDeviceName(configBackup?.device_id) }}</h3>
          <div class="flex gap-3">
            <button class="px-3 py-1.5 text-xs bg-gray-800 hover:bg-gray-700 rounded-lg text-gray-300 transition-colors" @click="copyConfig">复制</button>
            <button class="px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200" @click="showConfigDialog = false">关闭</button>
          </div>
        </div>
        <div class="flex-1 overflow-auto p-4">
          <pre class="text-xs text-gray-300 font-mono whitespace-pre-wrap">{{ configContent }}</pre>
        </div>
      </div>
    </div>

    <!-- Backup Result Dialog (备份全部设备结果明细) -->
    <div v-if="showBackupResult" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" @click.self="showBackupResult = false">
      <div class="bg-gray-900 rounded-xl border border-gray-700 w-[760px] max-h-[85vh] flex flex-col shadow-2xl">
        <div class="flex items-center justify-between px-6 py-4 border-b border-gray-800">
          <h3 class="text-lg font-bold">备份结果</h3>
          <button class="px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200" @click="showBackupResult = false">关闭</button>
        </div>
        <div class="px-6 py-3 border-b border-gray-800 text-sm">
          <span class="text-green-400 font-medium">成功 {{ backupSuccess }} 台</span>
          <span class="ml-6 text-red-400 font-medium">失败 {{ backupFailed }} 台</span>
          <span class="ml-6 text-gray-500">共 {{ backupResults.length }} 台</span>
        </div>
        <div class="flex-1 overflow-auto p-4">
          <!-- 失败设备明细 -->
          <div v-if="backupFailed > 0" class="mb-4">
            <div class="text-xs text-red-400 font-medium mb-2">失败设备与原因：</div>
            <div class="space-y-2">
              <div v-for="r in failedBackupResults" :key="r.device_id"
                   class="bg-red-950/30 border border-red-900/40 rounded-lg p-3">
                <div class="flex items-center justify-between gap-3">
                  <span class="text-gray-200 font-medium text-sm">{{ r.device_name }}</span>
                  <span class="text-gray-500 text-xs font-mono">{{ r.ip }}</span>
                </div>
                <div class="mt-1 text-red-400 text-xs break-all">{{ r.error || '未知原因' }}</div>
              </div>
            </div>
          </div>
          <div v-else class="text-green-400 text-sm py-6 text-center">全部设备备份成功</div>
        </div>
      </div>
    </div>

    <!-- Diff Viewer Dialog -->
    <div v-if="showDiffDialog" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" @click.self="showDiffDialog = false">
      <div class="bg-gray-900 rounded-xl border border-gray-700 w-[800px] max-h-[80vh] flex flex-col">
        <div class="flex items-center justify-between px-6 py-4 border-b border-gray-800">
          <h3 class="text-lg font-bold">配置对比</h3>
          <button class="px-3 py-1.5 text-xs text-gray-400 hover:text-gray-200" @click="showDiffDialog = false">关闭</button>
        </div>
        <div class="flex-1 overflow-auto p-4">
          <div v-for="(line, i) in diffResult" :key="i" class="font-mono text-xs py-0.5 px-2" :class="diffLineClass(line)">
            <span class="text-gray-600 select-none mr-2">{{ diffLinePrefix(line) }}</span>{{ line.content }}
          </div>
          <div v-if="diffResult.length === 0" class="text-center text-gray-500 py-8">配置内容完全一致</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'

const API = '/api/v1'

const activeTab = ref('backups')
const devices = ref([])
const backups = ref([])
const schedules = ref([])
const filterDeviceId = ref('')
const filterStatus = ref('')
const selectedBackups = ref([])
// 备份列表分页
const backupPage = ref(1)
const backupPageSize = ref(10)
const backupTotal = ref(0)
const backupTotalPages = computed(() => Math.max(1, Math.ceil(backupTotal.value / backupPageSize.value)))

// Dialogs
const showBackupDialog = ref(false)
const showScheduleDialog = ref(false)
const showConfigDialog = ref(false)
const showBackupResult = ref(false)
const backupResults = ref([])
const backupSuccess = computed(() => backupResults.value.filter(r => r.status === 'success').length)
const backupFailed = computed(() => backupResults.value.filter(r => r.status !== 'success').length)
const failedBackupResults = computed(() => backupResults.value.filter(r => r.status !== 'success'))
const showDiffDialog = ref(false)
const backing = ref(false)

const backupDeviceId = ref(null)
const editingSchedule = ref(null)
const scheduleForm = ref({ device_id: null, is_all_devices: false, frequency: 'daily', day_of_week: 0, day_of_month: 1, hour: 2, minute: 0 })
const configBackup = ref(null)
const configContent = ref('')
const diffResult = ref([])

onMounted(async () => {
  await loadDevices()
  await loadBackups()
  await loadSchedules()
})

async function loadDevices() {
  const res = await fetch(`${API}/devices?page_size=100`)
  const data = await res.json()
  devices.value = data.items || []
  if (devices.value.length > 0 && !backupDeviceId.value) {
    backupDeviceId.value = devices.value[0].id
    scheduleForm.value.device_id = devices.value[0].id
  }
}

async function loadBackups() {
  let url = `${API}/config-backups?page=${backupPage.value}&page_size=${backupPageSize.value}`
  if (filterDeviceId.value) url += `&device_id=${filterDeviceId.value}`
  if (filterStatus.value) url += `&status=${filterStatus.value}`
  const res = await fetch(url)
  const data = await res.json()
  backups.value = data.items || []
  backupTotal.value = data.total || 0
  selectedBackups.value = []
}

async function loadSchedules() {
  const res = await fetch(`${API}/config-backups/schedules/list`)
  const data = await res.json()
  schedules.value = data.items || []
}

function getDeviceName(id) {
  const d = devices.value.find(d => d.id === id)
  return d ? `${d.name} (${d.ip})` : `Device #${id}`
}

function formatTime(ts) {
  if (!ts) return '-'
  const d = new Date(ts)
  return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function freqLabel(f) {
  return { daily: '每天', weekly: '每周', monthly: '每月' }[f] || f
}

function scheduleTime(s) {
  const h = String(s.hour).padStart(2, '0')
  const m = String(s.minute).padStart(2, '0')
  if (s.frequency === 'daily') return `每天 ${h}:${m}`
  if (s.frequency === 'weekly') return `每周${['一','二','三','四','五','六','日'][s.day_of_week]} ${h}:${m}`
  if (s.frequency === 'monthly') return `每月${s.day_of_month}日 ${h}:${m}`
  return `${h}:${m}`
}

async function doManualBackup() {
  backing.value = true
  try {
    const res = await fetch(`${API}/config-backups/manual/${backupDeviceId.value}`, { method: 'POST' })
    if (res.ok) {
      await loadBackups()
      showBackupDialog.value = false
    } else {
      const err = await res.json()
      alert('备份失败: ' + (err.detail || '未知错误'))
    }
  } catch (e) {
    alert('请求失败: ' + e.message)
  }
  backing.value = false
}

async function backupAll() {
  if (!confirm(`确认备份全部 ${devices.value.length} 台设备？此操作可能需要一些时间。`)) return
  backing.value = true
  try {
    const res = await fetch(`${API}/config-backups/manual-all`, { method: 'POST' })
    if (res.ok) {
      const data = await res.json()
      backupResults.value = data.results || []
      showBackupResult.value = true
      await loadBackups()
    } else {
      const err = await res.json()
      alert('备份失败: ' + (err.detail || '未知错误'))
    }
  } catch (e) {
    alert('请求失败: ' + e.message)
  }
  backing.value = false
}

// 查看单条失败备份的原因
function showError(b) {
  alert(`备份失败原因（${getDeviceName(b.device_id)}）:\n${b.error_message || '未知原因'}`)
}

async function viewConfig(id) {
  const res = await fetch(`${API}/config-backups/${id}`)
  if (res.ok) {
    const data = await res.json()
    configBackup.value = data
    configContent.value = data.config_content
    showConfigDialog.value = true
  }
}

async function deleteBackup(id) {
  if (!confirm('确认删除此备份记录？')) return
  await fetch(`${API}/config-backups/${id}`, { method: 'DELETE' })
  await loadBackups()
}

function toggleSelect(id) {
  const idx = selectedBackups.value.indexOf(id)
  if (idx >= 0) {
    selectedBackups.value.splice(idx, 1)
  } else if (selectedBackups.value.length < 2) {
    selectedBackups.value.push(id)
  }
}

function toggleSelectAll(event) {
  if (event.target.checked) {
    selectedBackups.value = backups.value.filter(b => b.status === 'success').map(b => b.id).slice(0, 2)
  } else {
    selectedBackups.value = []
  }
}

async function compareSelected() {
  if (selectedBackups.value.length !== 2) return
  const [id1, id2] = selectedBackups.value
  const res = await fetch(`${API}/config-backups/compare/${id1}/${id2}`)
  if (res.ok) {
    const data = await res.json()
    diffResult.value = data.diff
    showDiffDialog.value = true
  }
}

function diffLineClass(line) {
  if (line.type === 'added') return 'bg-green-900/30 text-green-300'
  if (line.type === 'removed') return 'bg-red-900/30 text-red-300'
  if (line.type === 'hunk') return 'text-cyan-500 font-bold'
  return 'text-gray-400'
}

function diffLinePrefix(line) {
  if (line.type === 'added') return '+'
  if (line.type === 'removed') return '-'
  if (line.type === 'hunk') return '@'
  return ' '
}

function copyConfig() {
  const text = configContent.value
  // 生产环境常以 http://IP 访问（非安全上下文），navigator.clipboard 不可用，
  // 需走 textarea + execCommand 兜底；先判断安全上下文避免异步丢用户手势。
  const useClipboard = navigator.clipboard && window.isSecureContext
  if (useClipboard) {
    navigator.clipboard.writeText(text)
      .then(() => alert('已复制到剪贴板'))
      .catch(() => fallbackCopy(text))
  } else {
    fallbackCopy(text)
  }
}

function fallbackCopy(text) {
  const ok = copyWithExecCommand(text)
  alert(ok ? '已复制到剪贴板' : '复制失败，请手动选中文本后 Ctrl+C 复制')
}

function copyWithExecCommand(text) {
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    // 必须可见区域内才可选中；隐藏方式用固定定位 + 不透明而非 display:none
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
    return ok
  } catch (e) {
    return false
  }
}

function editSchedule(s) {
  editingSchedule.value = s
  scheduleForm.value = { device_id: s.device_id, is_all_devices: s.is_all_devices, frequency: s.frequency, day_of_week: s.day_of_week, day_of_month: s.day_of_month, hour: s.hour, minute: s.minute }
  showScheduleDialog.value = true
}

async function saveSchedule() {
  const payload = { ...scheduleForm.value }
  if (payload.frequency === 'daily') { payload.day_of_week = null; payload.day_of_month = null }
  if (payload.frequency === 'weekly') { payload.day_of_month = null }
  if (payload.frequency === 'monthly') { payload.day_of_week = null }
  // 全部设备模式不需要 device_id
  if (payload.is_all_devices) { payload.device_id = null }

  if (editingSchedule.value) {
    await fetch(`${API}/config-backups/schedules/${editingSchedule.value.id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
    })
  } else {
    await fetch(`${API}/config-backups/schedules`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
    })
  }
  editingSchedule.value = null
  showScheduleDialog.value = false
  await loadSchedules()
}

async function toggleSchedule(s) {
  await fetch(`${API}/config-backups/schedules/${s.id}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled: !s.enabled })
  })
  await loadSchedules()
}

async function deleteSchedule(id) {
  if (!confirm('确认删除此备份计划？')) return
  await fetch(`${API}/config-backups/schedules/${id}`, { method: 'DELETE' })
  await loadSchedules()
}
</script>
