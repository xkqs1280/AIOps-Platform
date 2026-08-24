<template>
  <div class="flex min-h-full flex-col bg-slate-950 px-6 pt-16">
    <div class="mb-8 text-center">
      <img src="../assets/logo.svg" alt="AIOps" class="mx-auto mb-3 h-16 w-16" />
      <h1 class="text-xl font-bold text-slate-100">AIOps 移动端</h1>
      <p class="mt-1 text-xs text-slate-500">智能运维托管平台</p>
    </div>

    <div class="space-y-4">
      <!-- 服务器地址 -->
      <div>
        <label class="mb-1.5 block text-xs text-slate-400">服务器地址</label>
        <div class="flex gap-2">
          <div class="flex flex-1 items-center gap-1 rounded-xl border border-slate-700 bg-slate-900 px-3">
            <span class="text-slate-500">https://</span>
            <input
              v-model="ip"
              type="text"
              inputmode="decimal"
              placeholder="192.168.1.100"
              class="w-full bg-transparent py-3 text-sm text-slate-100 outline-none placeholder:text-slate-600"
            />
          </div>
          <div class="flex items-center rounded-xl border border-slate-700 bg-slate-900 px-2">
            <span class="text-slate-500">:</span>
            <input
              v-model="port"
              type="number"
              placeholder="8000"
              class="w-14 bg-transparent py-3 text-sm text-slate-100 outline-none placeholder:text-slate-600"
            />
          </div>
        </div>
        <label class="mt-2 flex items-center gap-2 text-xs text-slate-400">
          <input v-model="remembered" type="checkbox" class="accent-cyan-500" />
          记住此服务器
        </label>
      </div>

      <!-- 账号 -->
      <div>
        <label class="mb-1.5 block text-xs text-slate-400">账号</label>
        <input
          v-model="username"
          type="text"
          autocomplete="username"
          placeholder="admin"
          class="w-full rounded-xl border border-slate-700 bg-slate-900 px-3 py-3 text-sm text-slate-100 outline-none placeholder:text-slate-600 focus:border-cyan-500"
        />
      </div>

      <!-- 密码 -->
      <div>
        <label class="mb-1.5 block text-xs text-slate-400">密码</label>
        <input
          v-model="password"
          type="password"
          autocomplete="current-password"
          placeholder="请输入密码"
          class="w-full rounded-xl border border-slate-700 bg-slate-900 px-3 py-3 text-sm text-slate-100 outline-none placeholder:text-slate-600 focus:border-cyan-500"
          @keyup.enter="doLogin"
        />
      </div>

      <div class="pt-2">
        <button
          @click="doLogin"
          :disabled="loading"
          class="w-full rounded-xl bg-cyan-600 py-3.5 text-sm font-semibold text-white transition-colors hover:bg-cyan-500 disabled:opacity-50"
        >
          {{ loading ? '登录中...' : '登 录' }}
        </button>
      </div>

      <p v-if="error" class="text-center text-xs text-red-400">{{ error }}</p>
    </div>

    <p class="mt-8 text-center text-[10px] text-slate-600">支持接入任意已部署的 AIOps 平台服务器</p>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { login } from '../api.js'
import { getServer, setServer, setToken } from '../store.js'
import { startAlertPolling } from '../notifications.js'

const router = useRouter()
const ip = ref('')
const port = ref(8000)
const remembered = ref(true)
const username = ref('admin')
const password = ref('')
const loading = ref(false)
const error = ref('')

onMounted(() => {
  const saved = getServer()
  if (saved) {
    ip.value = saved.ip || ''
    port.value = saved.port || 8000
    remembered.value = saved.remembered !== false
  }
})

async function doLogin() {
  const serverIp = ip.value.trim()
  if (!serverIp) { error.value = '请输入服务器地址'; return }
  if (!username.value.trim() || !password.value) { error.value = '请输入账号和密码'; return }
  loading.value = true
  error.value = ''
  try {
    const server = { ip: serverIp, port: Number(port.value) || 8000, remembered: remembered.value }
    setServer(server)
    // 登录时临时覆盖 api baseURL 指向该服务器
    const base = `https://${server.ip}${server.port ? ':' + server.port : ''}/api/v1`
    const res = await login({ username: username.value.trim(), password: password.value })
    setToken(res.access_token || '')
    startAlertPolling()
    router.push('/')
  } catch (e) {
    error.value = (e?.response?.data?.detail) || '登录失败，请检查服务器地址或账号密码'
  } finally {
    loading.value = false
  }
}
</script>
