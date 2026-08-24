<template>
  <div class="min-h-screen bg-slate-950 text-slate-100 p-4 flex flex-col gap-4 animate-in">
    <div class="flex items-center gap-3 px-2">
      <div class="w-2 h-8 bg-gradient-to-b from-yellow-400 to-orange-600 rounded-full"></div>
      <h1 class="text-2xl font-bold tracking-wide">设备生命周期管理</h1>
    </div>

    <!-- Tabs -->
    <div class="flex gap-1 bg-slate-900 rounded-lg p-1 w-fit">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        @click="activeTab = tab.key"
        class="px-5 py-2 rounded-md text-sm font-medium transition-colors"
        :class="activeTab === tab.key
          ? 'bg-cyan-500/20 text-cyan-400'
          : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'"
      >{{ tab.label }}</button>
    </div>

    <!-- Tab 1: Reminders -->
    <div v-if="activeTab === 'reminders'" class="flex flex-col gap-4">
      <div v-for="section in reminderSections" :key="section.key" class="bg-slate-900 rounded-xl border border-slate-800 overflow-hidden">
        <div class="flex items-center justify-between px-4 py-3" :class="section.headerBg">
          <div class="flex items-center gap-2">
            <span class="w-2.5 h-2.5 rounded-full" :class="section.dotColor"></span>
            <h3 class="text-sm font-semibold">{{ section.title }}</h3>
            <span class="text-xs opacity-70">({{ getReminderList(section.key).length }})</span>
          </div>
          <span class="text-xs opacity-60">{{ section.description }}</span>
        </div>
        <div class="overflow-x-auto">
          <table class="w-full text-sm" v-if="getReminderList(section.key).length">
            <thead>
              <tr class="border-b border-slate-800 text-slate-400 text-xs">
                <th class="text-left px-4 py-2.5 font-medium">设备名称</th>
                <th class="text-left px-4 py-2.5 font-medium">剩余天数</th>
                <th class="text-left px-4 py-2.5 font-medium">到期日期</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="(item, idx) in getReminderList(section.key)"
                :key="idx"
                class="border-b border-slate-800/50 hover:bg-slate-800/30 transition-colors"
              >
                <td class="px-4 py-2.5 text-slate-200">{{ item.device_name }}</td>
                <td class="px-4 py-2.5">
                  <span class="px-2 py-0.5 rounded text-xs font-medium" :class="section.badgeBg">{{ item.days_remaining }} 天</span>
                </td>
                <td class="px-4 py-2.5 text-slate-400">{{ item.date }}</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="px-4 py-10 text-center text-slate-500 text-sm">暂无数据</div>
        </div>
      </div>
    </div>

    <!-- Tab 2: 厂商生命周期数据库 -->
    <div v-if="activeTab === 'eos'" class="bg-slate-900 rounded-xl border border-slate-800 overflow-hidden">
      <div class="flex items-center justify-between px-4 py-3 border-b border-slate-800">
        <div class="flex items-center gap-2">
          <span class="w-2.5 h-2.5 bg-blue-400 rounded-full"></span>
          <h3 class="text-sm font-semibold">厂商生命周期数据库</h3>
        </div>
        <div class="flex gap-2">
          <button @click="seedDb" class="px-3 py-1.5 text-xs rounded-lg bg-purple-500/15 text-purple-400 border border-purple-500/30 hover:bg-purple-500/25 transition-colors">种子数据</button>
          <button @click="openAddModal" class="px-3 py-1.5 text-xs rounded-lg bg-cyan-500/15 text-cyan-400 border border-cyan-500/30 hover:bg-cyan-500/25 transition-colors">+ 添加记录</button>
        </div>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-sm" v-if="eosDb.length">
          <thead>
            <tr class="border-b border-slate-800 text-slate-400 text-xs">
              <th class="text-left px-4 py-2.5 font-medium">厂商</th>
              <th class="text-left px-4 py-2.5 font-medium">型号</th>
              <th class="text-left px-4 py-2.5 font-medium">维保到期</th>
              <th class="text-left px-4 py-2.5 font-medium">寿命到期</th>
              <th class="text-left px-4 py-2.5 font-medium">来源</th>
              <th class="text-right px-4 py-2.5 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in eosDb"
              :key="item.id"
              class="border-b border-slate-800/50 hover:bg-slate-800/30 transition-colors"
            >
              <td class="px-4 py-2.5 text-slate-200">{{ item.vendor }}</td>
              <td class="px-4 py-2.5 text-slate-300 font-mono text-xs">{{ item.model }}</td>
              <td class="px-4 py-2.5 text-slate-400">{{ item.eos_date }}</td>
              <td class="px-4 py-2.5 text-slate-400">{{ item.eol_date }}</td>
              <td class="px-4 py-2.5 text-slate-500 text-xs">{{ item.source }}</td>
              <td class="px-4 py-2.5 text-right">
                <button @click="openEditModal(item)" class="text-blue-400 hover:text-blue-300 text-xs mr-3 transition-colors">编辑</button>
                <button @click="deleteDbItem(item.id)" class="text-red-400 hover:text-red-300 text-xs transition-colors">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="px-4 py-10 text-center text-slate-500 text-sm">暂无数据，请添加或点击"种子数据"</div>
      </div>
    </div>

    <!-- Add/Edit Modal -->
    <div v-if="showModal" class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" @click.self="closeModal">
      <div class="bg-slate-900 border border-slate-700 rounded-xl w-full max-w-lg p-6 shadow-2xl max-h-[85vh] overflow-auto custom-scrollbar">
        <h3 class="text-lg font-semibold mb-4">{{ editingItem ? '编辑记录' : '添加记录（选择设备）' }}</h3>
        <div class="space-y-3">
          <!-- 编辑模式：厂商/型号直接输入 -->
          <template v-if="editingItem">
            <div>
              <label class="text-xs text-slate-400 mb-1 block">厂商</label>
              <input v-model="form.vendor" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:border-cyan-500 focus:outline-none" placeholder="如: H3C" />
            </div>
            <div>
              <label class="text-xs text-slate-400 mb-1 block">型号</label>
              <input v-model="form.model" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:border-cyan-500 focus:outline-none" placeholder="如: S5130S-28P-EI" />
            </div>
          </template>

          <!-- 添加模式：从已纳管设备选择（单台/批量） -->
          <template v-else>
            <div>
              <label class="text-xs text-slate-400 mb-1 block">选择已纳管设备（可多选）</label>
              <input
                v-model="deviceFilter"
                class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:border-cyan-500 focus:outline-none mb-1.5"
                placeholder="搜索设备名 / IP / 厂商 / 型号..."
              />
              <div class="max-h-40 overflow-auto border border-slate-700 rounded-lg bg-slate-800/40 divide-y divide-slate-800 custom-scrollbar">
                <label
                  v-for="d in filteredDevices"
                  :key="d.id"
                  class="flex items-center gap-2.5 px-3 py-1.5 cursor-pointer hover:bg-slate-700/40"
                >
                  <input type="checkbox" :value="d.id" v-model="selectedDeviceIds" class="accent-cyan-500" />
                  <span class="text-sm text-slate-200">{{ d.name }}</span>
                  <span class="text-xs text-slate-500 font-mono">{{ d.ip }}</span>
                  <span v-if="d.vendor || d.model" class="text-xs text-slate-600 ml-auto">
                    {{ d.vendor || '?' }} {{ d.model || '未识别型号' }}
                  </span>
                </label>
                <div v-if="filteredDevices.length === 0" class="px-3 py-4 text-center text-xs text-slate-500">无匹配设备</div>
              </div>
              <div class="mt-1 text-xs text-slate-500">已选 {{ selectedDeviceIds.length }} 台</div>
            </div>

            <!-- 型号去重预览 -->
            <div v-if="devicePreview.length" class="bg-blue-950/20 border border-blue-900/40 rounded-lg p-3">
              <div class="text-xs text-blue-300 mb-1.5">将按型号创建 {{ devicePreview.length }} 条厂商维保/寿命记录（同型号合并）并回写所选设备：</div>
              <div v-for="p in devicePreview" :key="`${p.vendor}|${p.model}`" class="text-xs text-slate-300 py-0.5">
                {{ p.vendor || '未识别厂商' }} <span class="text-slate-500">/</span> {{ p.model || '未识别型号' }}
                <span class="text-slate-500">× {{ p.count }} 台</span>
              </div>
              <div v-if="devicePreview.some(p => !p.model)" class="mt-1 text-[11px] text-yellow-500/80">含未识别型号的设备，仅回写设备维保/寿命日期，不生成厂商记录</div>
            </div>
          </template>

          <div>
            <label class="text-xs text-slate-400 mb-1 block">维保到期日期</label>
            <input v-model="form.eos_date" type="date" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:border-cyan-500 focus:outline-none" />
          </div>
          <div>
            <label class="text-xs text-slate-400 mb-1 block">设备寿命到期日期（一般按 8 年）</label>
            <input v-model="form.eol_date" type="date" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:border-cyan-500 focus:outline-none" />
          </div>
          <div>
            <label class="text-xs text-slate-400 mb-1 block">来源</label>
            <input v-model="form.source" class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:border-cyan-500 focus:outline-none" placeholder="如: H3C官方" />
          </div>
        </div>
        <div class="flex justify-end gap-2 mt-5">
          <button @click="closeModal" class="px-4 py-2 text-sm rounded-lg bg-slate-800 text-slate-400 hover:text-slate-200 transition-colors">取消</button>
          <button @click="submitForm" class="px-4 py-2 text-sm rounded-lg bg-cyan-500/20 text-cyan-400 border border-cyan-500/30 hover:bg-cyan-500/30 transition-colors">{{ editingItem ? '保存' : '添加' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, computed } from 'vue'
import { getDevices } from '../api/index.js'

const API_BASE = '/api/v1'

const activeTab = ref('reminders')
const tabs = [
  { key: 'reminders', label: '提醒列表' },
  { key: 'eos', label: '厂商生命周期数据库' },
]

const reminderSections = [
  {
    key: 'warranty_expiring',
    title: '保修即将到期',
    description: '7天内到期',
    headerBg: 'bg-red-900/20',
    dotColor: 'bg-red-500',
    badgeBg: 'bg-red-500/15 text-red-400',
  },
  {
    key: 'eos_approaching',
    title: '维保即将到期',
    description: '180天内到期',
    headerBg: 'bg-orange-900/15',
    dotColor: 'bg-orange-500',
    badgeBg: 'bg-orange-500/15 text-orange-400',
  },
  {
    key: 'eol_approaching',
    title: '设备寿命即将到期',
    description: '365天内到期',
    headerBg: 'bg-yellow-900/15',
    dotColor: 'bg-yellow-500',
    badgeBg: 'bg-yellow-500/15 text-yellow-400',
  },
  {
    key: 'eol_critical',
    title: '寿命到期关键设备',
    description: '180天内到期',
    headerBg: 'bg-red-950/30',
    dotColor: 'bg-red-400',
    badgeBg: 'bg-red-500/10 text-red-300',
  },
  {
    key: 'already_eol',
    title: '已过寿命设备',
    description: '已超过生命周期',
    headerBg: 'bg-slate-800/50',
    dotColor: 'bg-slate-500',
    badgeBg: 'bg-slate-600/30 text-slate-400',
  },
]

const reminders = ref({})
const eosDb = ref([])

// 已纳管设备（用于添加记录时选择）
const devices = ref([])
const selectedDeviceIds = ref([])
const deviceFilter = ref('')

async function loadDevices() {
  try {
    const res = await getDevices({ page_size: 300 })
    devices.value = (res.items || res.data?.items || []).map(d => ({
      id: d.id, name: d.name, ip: d.ip, vendor: d.vendor || '', model: d.model || '',
    }))
  } catch (err) {
    console.error('Failed to load devices:', err)
  }
}

// 选中设备的型号去重预览（决定将创建哪些厂商型号记录）
const devicePreview = computed(() => {
  const map = new Map()
  for (const d of devices.value) {
    if (!selectedDeviceIds.value.includes(d.id)) continue
    const key = `${d.vendor}|${d.model}`
    const rec = map.get(key) || { vendor: d.vendor, model: d.model, count: 0, devices: [] }
    rec.count += 1
    rec.devices.push(d)
    map.set(key, rec)
  }
  return [...map.values()]
})

const filteredDevices = computed(() => {
  const kw = deviceFilter.value.trim().toLowerCase()
  if (!kw) return devices.value
  return devices.value.filter(d =>
    d.name.toLowerCase().includes(kw) || d.ip.toLowerCase().includes(kw) ||
    (d.vendor || '').toLowerCase().includes(kw) || (d.model || '').toLowerCase().includes(kw)
  )
})

function getReminderList(key) {
  return reminders.value[key] || []
}

async function fetchReminders() {
  try {
    const res = await fetch(`${API_BASE}/lifecycle/reminders`)
    const data = await res.json()
    reminders.value = data.data || data || {}
  } catch (err) {
    console.error('Failed to fetch reminders:', err)
  }
}

async function fetchEosDb() {
  try {
    const res = await fetch(`${API_BASE}/lifecycle/db`)
    const data = await res.json()
    eosDb.value = data.data || data || []
  } catch (err) {
    console.error('Failed to fetch lifecycle DB:', err)
  }
}

// Seed data
async function seedDb() {
  try {
    await fetch(`${API_BASE}/lifecycle/seed`, { method: 'POST' })
    await fetchEosDb()
  } catch (err) {
    console.error('Failed to seed:', err)
  }
}

// Modal
const showModal = ref(false)
const editingItem = ref(null)
const form = ref({ vendor: '', model: '', eos_date: '', eol_date: '', source: '' })

function openAddModal() {
  editingItem.value = null
  form.value = { vendor: '', model: '', eos_date: '', eol_date: '', source: '' }
  selectedDeviceIds.value = []
  deviceFilter.value = ''
  if (devices.value.length === 0) loadDevices()
  showModal.value = true
}

function openEditModal(item) {
  editingItem.value = item
  form.value = { vendor: item.vendor, model: item.model, eos_date: item.eos_date, eol_date: item.eol_date, source: item.source }
  showModal.value = true
}

function closeModal() {
  showModal.value = false
  editingItem.value = null
}

async function submitForm() {
  try {
    if (editingItem.value) {
      const payload = { ...form.value }
      await fetch(`${API_BASE}/lifecycle/db/${editingItem.value.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
    } else if (selectedDeviceIds.value.length > 0) {
      // 从已纳管设备批量创建：走 /lifecycle/db/batch
      const res = await fetch(`${API_BASE}/lifecycle/db/batch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          device_ids: selectedDeviceIds.value,
          eos_date: form.value.eos_date || null,
          eol_date: form.value.eol_date || null,
          source: form.value.source || 'manual',
        }),
      })
      if (!res.ok) {
        const err = await res.json()
        alert('添加失败: ' + (err.detail || '未知错误'))
        return
      }
    } else {
      alert('请先选择要设置的设备')
      return
    }
    closeModal()
    await fetchEosDb()
    await fetchReminders()
  } catch (err) {
    console.error('Failed to submit form:', err)
    alert('添加失败: ' + err.message)
  }
}

async function deleteDbItem(id) {
  if (!confirm('确定删除此记录？')) return
  try {
    await fetch(`${API_BASE}/lifecycle/db/${id}`, { method: 'DELETE' })
    await fetchEosDb()
  } catch (err) {
    console.error('Failed to delete:', err)
  }
}

onMounted(() => {
  fetchReminders()
  fetchEosDb()
})
</script>
