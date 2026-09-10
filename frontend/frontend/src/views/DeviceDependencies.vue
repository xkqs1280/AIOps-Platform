<!--
  已取消（2026-09-10）：设备依赖改为**由拓扑连线自动推导**，不再手工配置。
  入口移至「拓扑发现」页（右侧面板「依赖关系」）；路由 /settings/device-dependencies 已重定向到 /topology。
  本文件仅作保留备份，当前不再被任何路由引用。
-->
<template>
  <div class="p-6 max-w-5xl mx-auto animate-in">
    <div class="flex items-center justify-between mb-6">
      <div>
        <h2 class="text-xl font-bold text-ink-strong">设备依赖</h2>
        <p class="text-sm text-ink-faint mt-1">
          声明「谁挂在哪里」，上游不可达时自动抑制下游的连带告警，避免一次断链刷出几十条告警
        </p>
      </div>
      <div class="flex items-center gap-2">
        <span v-if="isAdmin" class="px-2 py-0.5 rounded text-xs bg-cyan-500/10 text-cyan-400 border border-cyan-500/30">管理员</span>
        <button v-if="isAdmin" @click="openCreate" class="btn btn-primary">+ 添加依赖</button>
      </div>
    </div>

    <!-- 非管理员提示 -->
    <div v-if="!isAdmin" class="bg-surface border border-line rounded-xl p-8 text-center">
      <div class="text-3xl mb-3">🔒</div>
      <p class="text-sm text-ink-muted">仅管理员可查看和配置设备依赖</p>
    </div>

    <template v-else>
      <!-- 说明 -->
      <div class="bg-surface border border-line rounded-xl p-4 mb-5 text-xs text-ink-faint space-y-1">
        <p>· 语义：<b class="text-ink-muted">下游依赖上游</b>（如接入交换机依赖汇聚交换机）。</p>
        <p>· 上游离线或采集异常时，下游设备产生的告警会被标记为
          <b class="text-yellow-400">已抑制</b>——仍会入库可查，但不推送通知、不计入活跃告警数；上游恢复后自动解除。</p>
        <p>· <b class="text-ink-muted">严重级别告警不会被抑制</b>，避免关键故障被掩盖；环路（A→B→A）会被拒绝。</p>
        <p>· 未配置任何依赖时，抑制逻辑不生效（保持原有行为）。</p>
      </div>

      <!-- 提示条 -->
      <div v-if="msg" class="mb-4 px-4 py-2.5 rounded-lg text-sm border"
           :class="msgOk ? 'bg-green-500/10 text-green-400 border-green-500/30' : 'bg-red-500/10 text-red-400 border-red-500/30'">
        {{ msg }}
      </div>

      <div class="bg-surface border border-line rounded-xl overflow-hidden">
        <div v-if="loading" class="p-8 text-center text-sm text-ink-faint">加载中...</div>

        <div v-else-if="deps.length === 0" class="p-12 text-center">
          <div class="text-3xl mb-3">🔗</div>
          <p class="text-sm text-ink-muted mb-1">还没有配置设备依赖</p>
          <p class="text-xs text-ink-faint">配置后，链路故障只会产生上游的一条根因告警</p>
        </div>

        <table v-else class="w-full text-left text-sm">
          <thead class="border-b border-line bg-surface-2 text-ink-muted text-xs uppercase tracking-wider">
            <tr>
              <th class="px-4 py-3 font-medium">下游设备（接入口）</th>
              <th class="px-4 py-3 font-medium w-10"></th>
              <th class="px-4 py-3 font-medium">上游设备（汇聚口）</th>
              <th class="px-4 py-3 font-medium">启用</th>
              <th class="px-4 py-3 font-medium">备注</th>
              <th class="px-4 py-3 font-medium text-right">操作</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-line">
            <tr v-for="d in deps" :key="d.id" class="hover:bg-hover/30 transition-colors">
              <td class="px-4 py-3">
                <div class="font-medium text-ink-strong">{{ d.device_name }}</div>
                <div class="mt-0.5 text-xs text-ink-faint">#{{ d.device_id }}</div>
              </td>
              <td class="px-4 py-3 text-center text-ink-faint">→</td>
              <td class="px-4 py-3">
                <div class="font-medium text-ink-strong">{{ d.depends_on_name }}</div>
                <div class="mt-0.5 text-xs text-ink-faint">#{{ d.depends_on_device_id }}</div>
              </td>
              <td class="px-4 py-3">
                <button
                  @click="toggleEnabled(d)"
                  class="relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none"
                  :class="d.enabled ? 'bg-cyan-600' : 'bg-hover'"
                >
                  <span class="inline-block h-4 w-4 transform rounded-full bg-white transition-transform"
                        :class="d.enabled ? 'translate-x-6' : 'translate-x-1'" />
                </button>
              </td>
              <td class="px-4 py-3 text-xs text-ink-muted max-w-[16rem]">
                <span class="line-clamp-2 whitespace-normal" :title="d.note">{{ d.note || '-' }}</span>
              </td>
              <td class="px-4 py-3">
                <div class="flex items-center justify-end gap-1.5">
                  <button @click="openEdit(d)"
                          class="px-2.5 py-1 text-xs rounded text-blue-400 hover:bg-blue-600/20 transition-colors">
                    编辑
                  </button>
                  <button @click="askDelete(d)"
                          class="px-2.5 py-1 text-xs rounded text-red-400 hover:bg-red-600/20 transition-colors">
                    删除
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- 弹窗统一 Teleport 到 body：脱离页面容器，避免被祖先的 transform / overflow 影响定位或被裁切 -->
    <Teleport to="body">
    <!-- 新增 / 编辑弹窗 -->
    <div v-if="showModal" class="fixed inset-0 z-50 flex overflow-y-auto bg-black/60 p-4" @click.self="closeModal">
      <div class="m-auto w-full max-w-lg rounded-xl border border-line bg-surface shadow-2xl">
        <div class="border-b border-line px-6 py-4">
          <h2 class="text-lg font-semibold text-ink-strong">{{ isEditing ? '编辑依赖关系' : '添加依赖关系' }}</h2>
        </div>

        <div class="max-h-[65vh] space-y-4 overflow-y-auto px-6 py-4">
          <div>
            <label class="mb-1 block text-sm font-medium text-ink-muted">下游设备（产生连带告警的一方）</label>
            <select v-model.number="form.device_id"
                    class="w-full rounded-lg border border-line bg-surface-2 px-3 py-2 text-sm text-ink-strong focus:border-cyan-500 focus:outline-none focus:ring-1 focus:ring-cyan-500">
              <option :value="0" disabled>请选择设备</option>
              <option v-for="dev in devices" :key="dev.id" :value="dev.id" :disabled="dev.id === form.depends_on_device_id">
                {{ dev.name }}（{{ dev.ip }}）
              </option>
            </select>
          </div>

          <div class="flex justify-center text-ink-faint text-lg">↓ 依赖</div>

          <div>
            <label class="mb-1 block text-sm font-medium text-ink-muted">上游设备（不可达时抑制下游告警）</label>
            <select v-model.number="form.depends_on_device_id"
                    class="w-full rounded-lg border border-line bg-surface-2 px-3 py-2 text-sm text-ink-strong focus:border-cyan-500 focus:outline-none focus:ring-1 focus:ring-cyan-500">
              <option :value="0" disabled>请选择设备</option>
              <option v-for="dev in devices" :key="dev.id" :value="dev.id" :disabled="dev.id === form.device_id">
                {{ dev.name }}（{{ dev.ip }}）
              </option>
            </select>
          </div>

          <div>
            <label class="mb-1 block text-sm font-medium text-ink-muted">备注（可选）</label>
            <input v-model="form.note" type="text" class="input" placeholder="如：3F 接入交换机上联汇聚" />
          </div>

          <label class="flex items-center gap-2 text-sm text-ink-muted cursor-pointer">
            <input type="checkbox" v-model="form.enabled" class="accent-cyan-500" /> 启用此依赖关系
          </label>
        </div>

        <div class="flex justify-end gap-3 border-t border-line px-6 py-4">
          <button @click="closeModal" class="rounded-lg border border-line px-4 py-2 text-sm text-ink-muted hover:bg-hover transition-colors">
            取消
          </button>
          <button @click="submit" :disabled="saving" class="btn btn-primary">
            {{ saving ? '保存中...' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 删除确认 -->
    <div v-if="deleteTarget" class="fixed inset-0 z-50 flex overflow-y-auto bg-black/60 p-4" @click.self="deleteTarget = null">
      <div class="m-auto w-full max-w-sm rounded-xl border border-line bg-surface shadow-2xl">
        <div class="px-6 py-6">
          <h3 class="text-base font-semibold text-ink-strong">确认删除</h3>
          <p class="mt-2 text-sm text-ink-muted">
            确定删除「{{ deleteTarget.device_name }} → {{ deleteTarget.depends_on_name }}」这条依赖吗？
          </p>
          <p class="mt-1 text-xs text-ink-faint">删除后该下游设备的告警不再被抑制。</p>
        </div>
        <div class="flex justify-end gap-3 border-t border-line px-6 py-4">
          <button @click="deleteTarget = null" class="rounded-lg border border-line px-4 py-2 text-sm text-ink-muted hover:bg-hover transition-colors">
            取消
          </button>
          <button @click="confirmDelete" :disabled="deleting" class="btn btn-danger">
            {{ deleting ? '删除中...' : '删除' }}
          </button>
        </div>
      </div>
    </div>
    </Teleport>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import {
  getMe,
  getDevices,
  getDeviceDependencies,
  createDeviceDependency,
  updateDeviceDependency,
  deleteDeviceDependency,
} from '../api/index.js'

const isAdmin = ref(false)
const loading = ref(true)
const deps = ref([])
const devices = ref([])
const msg = ref('')
const msgOk = ref(true)

const showModal = ref(false)
const isEditing = ref(false)
const editingId = ref(null)
const saving = ref(false)
const deleteTarget = ref(null)
const deleting = ref(false)

const defaultForm = () => ({
  device_id: 0,
  depends_on_device_id: 0,
  enabled: true,
  note: '',
})
const form = reactive(defaultForm())

function flash(text, ok = true) {
  msg.value = text
  msgOk.value = ok
  setTimeout(() => { if (msg.value === text) msg.value = '' }, 4500)
}

async function fetchDevices() {
  try {
    const data = await getDevices({ page: 1, page_size: 300 })
    devices.value = data?.items || []
  } catch {
    devices.value = []
  }
}

async function fetchDeps() {
  loading.value = true
  try {
    const data = await getDeviceDependencies()
    deps.value = Array.isArray(data) ? data : []
  } catch (e) {
    deps.value = []
    flash('加载依赖失败：' + (e.response?.data?.detail || e.message), false)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  isEditing.value = false
  editingId.value = null
  Object.assign(form, defaultForm())
  showModal.value = true
}

function openEdit(d) {
  isEditing.value = true
  editingId.value = d.id
  Object.assign(form, {
    device_id: d.device_id,
    depends_on_device_id: d.depends_on_device_id,
    enabled: d.enabled,
    note: d.note || '',
  })
  showModal.value = true
}

function closeModal() { showModal.value = false }

async function submit() {
  if (!form.device_id || !form.depends_on_device_id) { flash('请选择下游与上游设备', false); return }
  if (form.device_id === form.depends_on_device_id) { flash('设备不能依赖自身', false); return }
  saving.value = true
  try {
    const payload = {
      device_id: form.device_id,
      depends_on_device_id: form.depends_on_device_id,
      enabled: form.enabled,
      note: form.note || null,
    }
    if (isEditing.value) {
      await updateDeviceDependency(editingId.value, payload)
      flash('依赖关系已更新')
    } else {
      await createDeviceDependency(payload)
      flash('依赖关系已添加')
    }
    showModal.value = false
    await fetchDeps()
  } catch (e) {
    // 环路由后端拒绝，直接把原因透出
    flash('保存失败：' + (e.response?.data?.detail || e.message || '未知错误'), false)
  } finally {
    saving.value = false
  }
}

async function toggleEnabled(d) {
  const next = !d.enabled
  try {
    await updateDeviceDependency(d.id, {
      device_id: d.device_id,
      depends_on_device_id: d.depends_on_device_id,
      enabled: next,
      note: d.note || null,
    })
    d.enabled = next
    flash(`已${next ? '启用' : '停用'}该依赖`)
  } catch (e) {
    flash('操作失败：' + (e.response?.data?.detail || e.message), false)
  }
}

function askDelete(d) { deleteTarget.value = d }

async function confirmDelete() {
  if (!deleteTarget.value) return
  deleting.value = true
  try {
    await deleteDeviceDependency(deleteTarget.value.id)
    flash('依赖关系已删除')
    deleteTarget.value = null
    await fetchDeps()
  } catch (e) {
    flash('删除失败：' + (e.response?.data?.detail || e.message), false)
  } finally {
    deleting.value = false
  }
}

onMounted(async () => {
  try {
    const me = await getMe()
    isAdmin.value = (me?.role || me?.data?.role) === 'admin'
  } catch { /* 未登录由全局拦截器处理 */ }
  if (!isAdmin.value) { loading.value = false; return }
  await fetchDevices()
  await fetchDeps()
})
</script>
