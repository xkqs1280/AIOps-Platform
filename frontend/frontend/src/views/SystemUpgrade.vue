<template>
  <div class="p-6 max-w-3xl mx-auto animate-in">
    <div class="flex items-center justify-between mb-6">
      <div>
        <h2 class="text-xl font-bold text-ink-strong">系统升级</h2>
        <p class="text-sm text-ink-faint mt-1">一键升级平台（保留设备与数据），上传官方升级包后自动完成备份、替换、重启</p>
      </div>
    </div>

    <!-- 版本信息 -->
    <div class="bg-surface border border-line rounded-xl p-6 mb-6">
      <div class="grid grid-cols-2 gap-4 text-sm">
        <div class="bg-surface-2/60 rounded-lg p-3">
          <div class="text-ink-faint text-xs mb-1">当前版本</div>
          <div class="text-ink-strong font-medium">{{ versionInfo.version || '—' }}</div>
        </div>
        <div class="bg-surface-2/60 rounded-lg p-3">
          <div class="text-ink-faint text-xs mb-1">构建时间</div>
          <div class="text-ink-strong font-medium">{{ versionInfo.build_time || '—' }}</div>
        </div>
      </div>
    </div>

    <!-- 升级进度（升级中/完成后显示） -->
    <div v-if="status.state && status.state !== 'idle'" class="bg-surface border border-line rounded-xl p-6 mb-6">
      <div class="flex items-center justify-between mb-3">
        <div>
          <span class="text-ink-strong font-semibold">{{ stateText }}</span>
          <span v-if="status.from_version && status.to_version" class="text-ink-faint text-sm ml-2">
            {{ status.from_version }} → {{ status.to_version }}
          </span>
        </div>
        <span v-if="status.error" class="text-red-400 text-sm">{{ status.error }}</span>
      </div>

      <!-- 进度条 -->
      <div class="w-full h-2.5 bg-surface-2 rounded-full overflow-hidden mb-4">
        <div
          class="h-full rounded-full transition-all duration-500"
          :class="progressColor"
          :style="{ width: (status.progress || 0) + '%' }"
        ></div>
      </div>

      <div class="text-sm text-ink-muted mb-3">{{ status.message || '等待中…' }}</div>

      <!-- 日志 -->
      <div class="bg-black/40 border border-line rounded-lg p-3 max-h-52 overflow-y-auto font-mono text-xs space-y-1">
        <div v-for="(line, i) in status.log" :key="i" class="text-ink-muted">{{ line }}</div>
      </div>

      <!-- 完成后操作 -->
      <div v-if="status.state === 'done' || status.state === 'rolled_back'" class="mt-4">
        <p class="text-sm text-emerald-400 mb-2">升级已完成，平台运行正常。建议刷新页面确认新版本。</p>
        <button
          @click="reloadPage"
          class="px-4 py-2 text-sm rounded-lg bg-cyan-600 hover:bg-cyan-500 text-white transition-colors"
        >刷新页面</button>
      </div>
      <div v-if="status.state === 'failed' && status.rollback_available" class="mt-4">
        <p class="text-sm text-red-400 mb-2">升级失败，可一键回滚到升级前版本。</p>
        <button
          @click="doRollback"
          :disabled="busy"
          class="px-4 py-2 text-sm rounded-lg bg-orange-600 hover:bg-orange-500 text-white transition-colors disabled:opacity-50"
        >{{ busy ? '回滚中…' : '回滚到上一版本' }}</button>
      </div>
    </div>

    <!-- 上传升级包 -->
    <div class="bg-surface border border-line rounded-xl p-6">
      <h3 class="font-semibold text-ink-strong mb-1.5">上传升级包</h3>
      <p class="text-sm text-ink-faint mb-4">
        从厂商获取 <code class="text-cyan-400">aiops-upgrade-vX.Y.Z.zip</code> 升级包后上传。升级会自动备份
        当前程序、配置与数据库快照，保留全部设备与历史数据；失败自动回滚。
      </p>

      <div
        class="border-2 border-dashed border-line hover:border-cyan-600/60 rounded-xl p-8 text-center cursor-pointer transition-colors"
        :class="{ 'opacity-50 pointer-events-none': busy || upgrading }"
        @click="fileInput.click()"
        @dragover.prevent
        @drop.prevent="onDrop"
      >
        <input ref="fileInput" type="file" accept=".zip" class="hidden" @change="onPick" />
        <div class="text-4xl mb-2">📦</div>
        <div class="text-sm text-ink-muted mb-1">{{ pickedFile ? pickedFile.name : '点击选择或拖拽升级包到此处' }}</div>
        <div v-if="pickedFile" class="text-xs text-ink-faint">约 {{ (pickedFile.size / 1024 / 1024).toFixed(1) }} MB</div>
      </div>

      <div class="flex items-center gap-3 mt-5">
        <button
          @click="doUpgrade"
          :disabled="!pickedFile || busy || upgrading"
          class="px-5 py-2.5 text-sm font-medium rounded-lg bg-cyan-600 hover:bg-cyan-500 text-white transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {{ upgrading ? '升级中…' : '一键升级' }}
        </button>
        <span v-if="busy" class="text-sm text-ink-muted">正在处理，请勿关闭页面…</span>
      </div>

      <p v-if="errorMsg" class="text-sm text-red-400 mt-3">{{ errorMsg }}</p>
    </div>

    <p class="text-[11px] text-ink-faint mt-4">
      升级说明：升级期间平台将短暂停机（约 30~60 秒），完成后自动恢复。升级不会清除任何设备、告警或配置数据。
    </p>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { getSystemVersion, startUpgrade, getUpgradeStatus, requestRollback } from '../api/index.js'

const versionInfo = ref({})
const status = ref({ state: 'idle', progress: 0, message: '', log: [] })
const pickedFile = ref(null)
const fileInput = ref(null)
const busy = ref(false)
const upgrading = ref(false)
const errorMsg = ref('')
let pollTimer = null

const stateText = computed(() => {
  const map = {
    idle: '空闲',
    uploading: '准备中',
    validating: '校验升级包',
    backup: '备份中（程序/配置/数据库）',
    applying: '停止服务',
    replacing: '替换文件',
    restarting: '启动新版本',
    verifying: '健康检查',
    done: '升级完成',
    failed: '升级失败',
    rolled_back: '已回滚',
  }
  return map[status.value.state] || status.value.state
})

const progressColor = computed(() => {
  const s = status.value.state
  if (s === 'done' || s === 'rolled_back') return 'bg-emerald-500'
  if (s === 'failed') return 'bg-red-500'
  return 'bg-cyan-500'
})

async function loadVersion() {
  try {
    versionInfo.value = await getSystemVersion()
  } catch { /* 忽略 */ }
}

async function pollStatus() {
  try {
    const s = await getUpgradeStatus()
    status.value = s || { state: 'idle', progress: 0, message: '', log: [] }
    // 升级中持续轮询；完成/失败/回滚后停止
    const active = ['uploading', 'validating', 'backup', 'applying', 'replacing', 'restarting', 'verifying']
    if (!active.includes(status.value.state)) {
      stopPoll()
      upgrading.value = false
      busy.value = false
    }
  } catch {
    // 服务重启期间接口可能暂时不可达，继续重试
  }
}

function startPoll() {
  stopPoll()
  pollTimer = setInterval(pollStatus, 2500)
}

function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function onPick(e) {
  const f = e.target.files && e.target.files[0]
  if (f) pickedFile.value = f
  errorMsg.value = ''
}

function onDrop(e) {
  const f = e.dataTransfer.files && e.dataTransfer.files[0]
  if (f) pickedFile.value = f
  errorMsg.value = ''
}

async function doUpgrade() {
  if (!pickedFile.value || busy.value) return
  busy.value = true
  upgrading.value = true
  errorMsg.value = ''
  try {
    await startUpgrade(pickedFile.value)
    status.value = { state: 'uploading', progress: 5, message: '升级已开始…', log: [] }
    startPoll()
  } catch (e) {
    errorMsg.value = (e.response && e.response.data && e.response.data.detail) || '升级包上传失败，请检查文件'
    upgrading.value = false
    busy.value = false
  }
}

async function doRollback() {
  if (busy.value) return
  busy.value = true
  errorMsg.value = ''
  try {
    await requestRollback()
    startPoll()
  } catch (e) {
    errorMsg.value = (e.response && e.response.data && e.response.data.detail) || '回滚启动失败'
    busy.value = false
  }
}

function reloadPage() {
  window.location.reload()
}

onMounted(() => {
  loadVersion()
  pollStatus() // 打开页面时先同步一次状态（可能上次升级未完）
  startPoll()
})
onUnmounted(stopPoll)
</script>
