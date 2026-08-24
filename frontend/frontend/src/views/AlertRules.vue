<template>
  <div class="min-h-screen bg-app text-ink-strong animate-in">
    <!-- Header -->
    <div class="border-b border-line bg-surface/50 px-6 py-4">
      <div class="flex items-center justify-between">
        <div>
          <h1 class="text-2xl font-bold text-ink-strong">告警规则管理</h1>
          <p class="mt-1 text-sm text-ink-muted">配置和管理监控告警规则</p>
        </div>
        <button
          @click="openCreateModal"
          class="btn btn-primary"
        >
          + 添加规则
        </button>
      </div>
    </div>

    <!-- Rules Table -->
    <div class="px-6 py-6">
      <div class="overflow-hidden rounded-xl border border-line bg-surface/50">
        <table class="w-full text-left text-sm">
          <thead class="border-b border-line bg-surface text-ink-muted">
            <tr>
              <th class="px-4 py-3 font-medium">规则名称</th>
              <th class="px-4 py-3 font-medium">监控指标</th>
              <th class="px-4 py-3 font-medium">条件</th>
              <th class="px-4 py-3 font-medium">阈值</th>
              <th class="px-4 py-3 font-medium">持续时间</th>
              <th class="px-4 py-3 font-medium">严重级别</th>
              <th class="px-4 py-3 font-medium">启用状态</th>
              <th class="px-4 py-3 font-medium text-right">操作</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-line">
            <tr
              v-for="rule in rules"
              :key="rule.id"
              class="transition-colors hover:bg-hover/30"
            >
              <td class="px-4 py-3">
                <div class="font-medium text-ink-strong">{{ rule.name }}</div>
                <div class="mt-0.5 text-xs text-ink-faint">{{ rule.description }}</div>
              </td>
              <td class="px-4 py-3 text-ink-muted">{{ metricLabel(rule.metric) }}</td>
              <td class="px-4 py-3">
                <span class="inline-flex items-center rounded-md bg-surface-2 px-2 py-1 text-xs font-medium text-ink-muted">
                  {{ conditionLabel(rule.condition) }}
                </span>
              </td>
              <td class="px-4 py-3 font-mono text-ink">{{ rule.threshold }}</td>
              <td class="px-4 py-3 text-ink-muted">{{ rule.duration }}s</td>
              <td class="px-4 py-3">
                <span :class="severityBadgeClass(rule.severity)" class="inline-flex items-center rounded-md px-2 py-1 text-xs font-medium">
                  {{ severityLabel(rule.severity) }}
                </span>
              </td>
              <td class="px-4 py-3">
                <button
                  @click="toggleEnabled(rule)"
                  :class="rule.enabled ? 'bg-brand-500' : 'bg-line-strong'"
                  class="relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 focus:ring-offset-slate-950"
                >
                  <span
                    :class="rule.enabled ? 'translate-x-6' : 'translate-x-1'"
                    class="inline-block h-4 w-4 transform rounded-full bg-white transition-transform"
                  />
                </button>
              </td>
              <td class="px-4 py-3">
                <div class="flex items-center justify-end gap-2">
                  <button
                    @click="openEditModal(rule)"
                    class="rounded-md px-2 py-1 text-xs text-blue-400 transition-colors hover:bg-blue-600/20 hover:text-blue-300"
                  >
                    编辑
                  </button>
                  <button
                    @click="openDeleteDialog(rule)"
                    class="rounded-md px-2 py-1 text-xs text-red-400 transition-colors hover:bg-red-600/20 hover:text-red-300"
                  >
                    删除
                  </button>
                </div>
              </td>
            </tr>
            <tr v-if="rules.length === 0">
              <td colspan="8" class="px-4 py-12 text-center text-ink-faint">
                暂无告警规则，点击"添加规则"创建
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Add/Edit Modal -->
    <div
      v-if="showModal"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      @click.self="closeModal"
    >
      <div class="w-full max-w-lg rounded-xl border border-line bg-surface shadow-2xl">
        <div class="border-b border-line px-6 py-4">
          <h2 class="text-lg font-semibold text-ink-strong">
            {{ isEditing ? '编辑规则' : '添加规则' }}
          </h2>
        </div>
        <div class="max-h-[60vh] space-y-4 overflow-y-auto px-6 py-4">
          <div>
            <label class="mb-1 block text-sm font-medium text-ink-muted">规则名称</label>
            <input
              v-model="formData.name"
              type="text"
              class="input"
              placeholder="请输入规则名称"
            />
          </div>
          <div>
            <label class="mb-1 block text-sm font-medium text-ink-muted">监控指标</label>
            <select
              v-model="formData.metric"
              class="w-full rounded-lg border border-line bg-surface-2 px-3 py-2 text-sm text-ink-strong focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            >
              <option value="" disabled>请选择监控指标</option>
              <option value="cpu_usage">CPU 利用率 (%)</option>
              <option value="memory_usage">内存利用率 (%)</option>
              <option value="temperature">温度 (°C)</option>
              <option value="sys_uptime">设备重启 (uptime)</option>
              <option value="if_oper_status">接口 Down</option>
              <option value="if_in_errors">入向错包速率 (个/秒)</option>
              <option value="if_out_errors">出向错包速率 (个/秒)</option>
              <option value="if_in_discards">入向丢弃速率 (个/秒)</option>
              <option value="if_out_discards">出向丢弃速率 (个/秒)</option>
            </select>
            <p class="mt-1 text-xs text-ink-faint">错包/丢弃指标：检测接口计数器增长速率，需设备支持 IF-MIB</p>
          </div>
          <div class="grid grid-cols-2 gap-4">
            <div>
              <label class="mb-1 block text-sm font-medium text-ink-muted">条件</label>
              <select
                v-model="formData.condition"
                class="w-full rounded-lg border border-line bg-surface-2 px-3 py-2 text-sm text-ink-strong focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
              >
                <option value="gt">大于 (gt)</option>
                <option value="lt">小于 (lt)</option>
                <option value="eq">等于 (eq)</option>
                <option value="delta">突变 (delta)</option>
              </select>
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium text-ink-muted">阈值</label>
              <input
                v-model.number="formData.threshold"
                type="number"
                class="input"
                placeholder="0"
              />
            </div>
          </div>
          <div class="grid grid-cols-2 gap-4">
            <div>
              <label class="mb-1 block text-sm font-medium text-ink-muted">持续时间 (秒)</label>
              <input
                v-model.number="formData.duration"
                type="number"
                class="input"
                placeholder="60"
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium text-ink-muted">严重级别</label>
              <select
                v-model="formData.severity"
                class="w-full rounded-lg border border-line bg-surface-2 px-3 py-2 text-sm text-ink-strong focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
              >
                <option value="critical">严重</option>
                <option value="major">重要</option>
                <option value="minor">次要</option>
                <option value="warning">警告</option>
              </select>
            </div>
          </div>
          <div>
            <label class="mb-1 block text-sm font-medium text-ink-muted">描述</label>
            <textarea
              v-model="formData.description"
              rows="3"
              class="input"
              placeholder="规则描述信息"
            />
          </div>
          <div class="flex items-center gap-2">
            <input
              v-model="formData.enabled"
              type="checkbox"
              class="h-4 w-4 rounded border-line-strong bg-surface-2 text-blue-600 focus:ring-blue-500"
            />
            <label class="text-sm text-ink-muted">启用此规则</label>
          </div>
        </div>
        <div class="flex justify-end gap-3 border-t border-line px-6 py-4">
          <button
            @click="closeModal"
            class="rounded-lg border border-line px-4 py-2 text-sm font-medium text-ink-muted transition-colors hover:bg-hover"
          >
            取消
          </button>
          <button
            @click="submitForm"
            :disabled="formSubmitting"
            class="btn btn-primary"
          >
            {{ formSubmitting ? '保存中...' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <!-- Delete Confirmation Dialog -->
    <div
      v-if="showDeleteDialog"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      @click.self="closeDeleteDialog"
    >
      <div class="w-full max-w-sm rounded-xl border border-line bg-surface shadow-2xl">
        <div class="px-6 py-6">
          <div class="flex items-center gap-3">
            <div class="flex h-10 w-10 items-center justify-center rounded-full bg-red-600/20">
              <svg class="h-5 w-5 text-red-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
              </svg>
            </div>
            <div>
              <h3 class="text-base font-semibold text-ink-strong">确认删除</h3>
              <p class="mt-1 text-sm text-ink-muted">
                确定要删除规则 "{{ deleteTarget?.name }}" 吗？此操作不可撤销。
              </p>
            </div>
          </div>
        </div>
        <div class="flex justify-end gap-3 border-t border-line px-6 py-4">
          <button
            @click="closeDeleteDialog"
            class="rounded-lg border border-line px-4 py-2 text-sm font-medium text-ink-muted transition-colors hover:bg-hover"
          >
            取消
          </button>
          <button
            @click="confirmDelete"
            :disabled="deleteSubmitting"
            class="rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-red-700 disabled:opacity-50"
          >
            {{ deleteSubmitting ? '删除中...' : '删除' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, reactive } from 'vue'
import {
  getAlertRules,
  createAlertRule,
  updateAlertRule,
  deleteAlertRule
} from '../api/index.js'

const rules = ref([])
const loading = ref(false)

// Modal state
const showModal = ref(false)
const isEditing = ref(false)
const editingId = ref(null)
const formSubmitting = ref(false)

// Delete dialog state
const showDeleteDialog = ref(false)
const deleteTarget = ref(null)
const deleteSubmitting = ref(false)

const defaultForm = () => ({
  name: '',
  metric: '',
  condition: 'gt',
  threshold: 0,
  duration: 60,
  severity: 'warning',
  enabled: true,
  description: ''
})

const formData = reactive(defaultForm())

const conditionMap = {
  gt: '大于',
  lt: '小于',
  eq: '等于',
  delta: '突变'
}

const conditionLabel = (condition) => conditionMap[condition] || condition

const severityMap = {
  critical: { label: '严重', class: 'bg-red-600/20 text-red-400 border border-red-600/30' },
  major: { label: '重要', class: 'bg-orange-600/20 text-orange-400 border border-orange-600/30' },
  minor: { label: '次要', class: 'bg-yellow-600/20 text-yellow-400 border border-yellow-600/30' },
  warning: { label: '警告', class: 'bg-blue-600/20 text-blue-400 border border-blue-600/30' }
}

const severityLabel = (severity) => severityMap[severity]?.label || severity
const severityBadgeClass = (severity) => severityMap[severity]?.class || ''

const metricMap = {
  cpu_usage: 'CPU 利用率',
  memory_usage: '内存利用率',
  temperature: '温度',
  sys_uptime: '设备重启',
  if_oper_status: '接口 Down',
  if_in_errors: '入向错包速率',
  if_out_errors: '出向错包速率',
  if_in_discards: '入向丢弃速率',
  if_out_discards: '出向丢弃速率'
}
const metricLabel = (metric) => metricMap[metric] || metric

const fetchRules = async () => {
  loading.value = true
  try {
    const data = await getAlertRules()
    rules.value = Array.isArray(data) ? data : []
  } catch (error) {
    console.error('获取告警规则失败:', error)
    rules.value = []
  } finally {
    loading.value = false
  }
}

const openCreateModal = () => {
  isEditing.value = false
  editingId.value = null
  Object.assign(formData, defaultForm())
  showModal.value = true
}

const openEditModal = (rule) => {
  isEditing.value = true
  editingId.value = rule.id
  Object.assign(formData, {
    name: rule.name,
    metric: rule.metric,
    condition: rule.condition,
    threshold: rule.threshold,
    duration: rule.duration,
    severity: rule.severity,
    enabled: rule.enabled,
    description: rule.description || ''
  })
  showModal.value = true
}

const closeModal = () => {
  showModal.value = false
}

const submitForm = async () => {
  if (!formData.name || !formData.metric) {
    return
  }
  formSubmitting.value = true
  try {
    const payload = { ...formData }
    if (isEditing.value) {
      await updateAlertRule(editingId.value, payload)
    } else {
      await createAlertRule(payload)
    }
    showModal.value = false
    await fetchRules()
  } catch (error) {
    console.error('保存规则失败:', error)
  } finally {
    formSubmitting.value = false
  }
}

const toggleEnabled = async (rule) => {
  try {
    await updateAlertRule(rule.id, { ...rule, enabled: !rule.enabled })
    rule.enabled = !rule.enabled
  } catch (error) {
    console.error('切换启用状态失败:', error)
  }
}

const openDeleteDialog = (rule) => {
  deleteTarget.value = rule
  showDeleteDialog.value = true
}

const closeDeleteDialog = () => {
  showDeleteDialog.value = false
  deleteTarget.value = null
}

const confirmDelete = async () => {
  if (!deleteTarget.value) return
  deleteSubmitting.value = true
  try {
    await deleteAlertRule(deleteTarget.value.id)
    showDeleteDialog.value = false
    deleteTarget.value = null
    await fetchRules()
  } catch (error) {
    console.error('删除规则失败:', error)
  } finally {
    deleteSubmitting.value = false
  }
}

onMounted(() => {
  fetchRules()
})
</script>
