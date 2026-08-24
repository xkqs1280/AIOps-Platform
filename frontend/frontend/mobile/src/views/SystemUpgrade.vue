<template>
  <div class="p-4">
    <header class="mb-4 flex items-center gap-3">
      <button @click="$router.back()" class="rounded-lg border border-slate-700 px-2.5 py-1.5 text-sm text-slate-300">‹ 返回</button>
      <h1 class="text-lg font-bold text-slate-100">系统升级</h1>
    </header>

    <!-- 版本信息 -->
    <div class="mb-4 rounded-2xl border border-slate-800 bg-slate-900 p-4">
      <div class="flex justify-between text-sm">
        <div>
          <p class="text-xs text-slate-500">当前版本</p>
          <p class="mt-0.5 font-medium text-slate-100">{{ versionInfo.version || '—' }}</p>
        </div>
        <div class="text-right">
          <p class="text-xs text-slate-500">构建时间</p>
          <p class="mt-0.5 text-slate-300">{{ versionInfo.build_time || '—' }}</p>
        </div>
      </div>
    </div>

    <!-- 升级进度 -->
    <div v-if="status.state && status.state !== 'idle'" class="mb-4 rounded-2xl border border-slate-800 bg-slate-900 p-4">
      <p class="text-sm font-medium text-slate-100">{{ stateText }}</p>
      <div class="mt-2 h-2 w-full overflow-hidden rounded-full bg-slate-800">
        <div
          class="h-full rounded-full transition-all duration-500"
          :class="progressColor"
          :style="{ width: (status.progress || 0) + '%' }"
        ></div>
      </div>
      <p class="mt-2 text-xs text-slate-400">{{ status.message }}</p>
      <div class="mt-2 max-h-40 overflow-y-auto rounded-lg bg-black/40 p-2 font-mono text-[10px] leading-relaxed text-slate-500">
        <div v-for="(line, i) in status.log" :key="i">{{ line }}</div>
      </div>
      <button
        v-if="status.state === 'done' || status.state === 'rolled_back'"
        @click="reloadPage"
        class="mt-3 rounded-lg bg-cyan-600 px-4 py-2 text-xs text-white"
      >刷新页面</button>
      <button
        v-if="status.state === 'failed' && status.rollback_available"
        @click="doRollback"
        :disabled="busy"
        class="mt-3 rounded-lg bg-orange-600 px-4 py-2 text-xs text-white disabled:opacity-50"
      >{{ busy ? '回滚中…' : '回滚到上一版本' }}</button>
    </div>

    <!-- 上传 -->
    <div class="rounded-2xl border border-slate-800 bg-slate-900 p-4">
      <p class="text-sm font-medium text-slate-100">上传升级包</p>
      <p class="mt-1 text-xs text-slate-500">选择官方升级包（.zip）后点击升级，自动备份并保留设备与数据。</p>
      <input
        type="file"
        accept=".zip"
        class="mt-3 w-full rounded-lg border border-slate-700 bg-slate-800 px-2 py-2 text-xs text-slate-300"
        @change="onPick"
      />
      <button
        @click="doUpgrade"
        :disabled="!pickedFile || busy || upgrading"
        class="mt-3 w-full rounded-xl bg-cyan-600 py-2.5 text-sm font-medium text-white disabled:opacity-40"
      >{{ upgrading ? '升级中…' : '一键升级' }}</button>
      <p v-if="errorMsg" class="mt-2 text-xs text-red-400">{{ errorMsg }}</p>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { getSystemVersion, startUpgrade, getUpgradeStatus, requestRollback } from '../api.js'

const versionInfo = ref({})
const status = ref({ state: 'idle', progress: 0, message: '', log: [] })
const pickedFile = ref(null)
const busy = ref(false)
const upgrading = ref(false)
const errorMsg = ref('')
let pollTimer = null

const stateText = computed(() => {
  const map = { idle: '空闲', uploading: '准备中', validating: '校验升级包', backup: '备份中', applying: '停止服务', replacing: '替换文件', restarting: '启动新版本', verifying: '健康检查', done: '升级完成', failed: '升级失败', rolled_back: '已回滚' }
  return map[status.value.state] || status.value.state
})
const progressColor = computed(() => {
  const s = status.value.state
  if (s === 'done' || s === 'rolled_back') return 'bg-emerald-500'
  if (s === 'failed') return 'bg-red-500'
  return 'bg-cyan-500'
})

function onPick(e) {
  const f = e.target.files && e.target.files[0]
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
    errorMsg.value = (e.response && e.response.data && e.response.data.detail) || '上传失败'
    upgrading.value = false
    busy.value = false
  }
}

async function doRollback() {
  if (busy.value) return
  busy.value = true
  try {
    await requestRollback()
    startPoll()
  } catch (e) {
    errorMsg.value = (e.response && e.response.data && e.response.data.detail) || '回滚启动失败'
    busy.value = false
  }
}

async function pollStatus() {
  try {
    const s = await getUpgradeStatus()
    status.value = s || { state: 'idle', progress: 0, message: '', log: [] }
    const active = ['uploading', 'validating', 'backup', 'applying', 'replacing', 'restarting', 'verifying']
    if (!active.includes(status.value.state)) {
      stopPoll()
      upgrading.value = false
      busy.value = false
    }
  } catch { /* 服务重启期间继续重试 */ }
}
function startPoll() {
  stopPoll()
  pollTimer = setInterval(pollStatus, 2500)
}
function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

function reloadPage() {
  window.location.reload()
}

onMounted(() => {
  getSystemVersion().then((v) => { versionInfo.value = v }).catch(() => {})
  pollStatus()
  startPoll()
})
onUnmounted(stopPoll)
</script>
