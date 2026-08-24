<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { login, getLicenseStatus } from '../api/index.js'

const router = useRouter()
const username = ref('')
const password = ref('')
const errMsg = ref('')
const loading = ref(false)

// 授权提示（未激活/到期/临期）
const licMsg = ref('')
const licClass = ref('')

onMounted(async () => {
  try {
    const s = await getLicenseStatus()
    if (s?.enabled) {
      let cls = ''
      let txt = ''
      if (!s.activated) {
        cls = 'bg-red-900/30 border-red-800/60 text-red-300'
        txt = '⚠ 平台未授权，登录后请前往「授权管理」激活（联系邮箱 x1280455974@163.com）'
      } else if (s.locked) {
        cls = 'bg-red-900/30 border-red-800/60 text-red-300'
        txt = '⚠ 平台授权已到期并锁定，请前往「授权管理」激活（联系邮箱 x1280455974@163.com）'
      } else if (!s.permanent && s.days_left !== null && s.days_left <= 30) {
        cls = 'bg-amber-900/30 border-amber-800/60 text-amber-300'
        txt = `⚠ 平台授权将于 ${(s.expires_at || '').slice(0, 10)} 到期（剩余 ${s.days_left} 天），请及时续期（联系邮箱 x1280455974@163.com）`
      }
      licMsg.value = txt
      licClass.value = cls
    }
  } catch { /* 忽略 */ }
})

async function doLogin() {
  if (!username.value || !password.value) {
    errMsg.value = '请输入用户名和密码'
    return
  }
  errMsg.value = ''
  loading.value = true
  // 音频解锁统一由系统内「激活语音」按钮 / 页面首次点击触发（浏览器安全策略要求目标页面上的用户手势）
  try {
    await login({ username: username.value, password: password.value })
    router.push('/')
  } catch (e) {
    errMsg.value = e.response?.data?.detail || e.message || '登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div
    class="min-h-screen bg-slate-950 flex items-center justify-center px-4 animate-fade"
    style="background-image: radial-gradient(80% 60% at 50% 0%, rgba(14, 165, 233, 0.12) 0%, rgba(2, 6, 23, 0) 55%), radial-gradient(60% 50% at 85% 90%, rgba(34, 211, 238, 0.08) 0%, rgba(2, 6, 23, 0) 60%)"
  >
    <div class="w-full max-w-sm">
      <div
        v-if="licMsg"
        class="mb-4 px-4 py-3 rounded-xl border text-sm leading-relaxed"
        :class="licClass"
      >
        {{ licMsg }}
      </div>

      <form
        class="bg-slate-900 border border-slate-800 rounded-2xl p-8 shadow-2xl grad-border animate-in"
        @submit.prevent="doLogin"
      >
        <div class="flex flex-col items-center mb-6">
          <img src="/logo.svg" class="w-14 h-14" alt="AIOps" />
          <h1 class="text-xl font-bold text-brand-400 mt-3">AIOps 平台登录</h1>
          <p class="text-xs text-slate-500 mt-1">网络及安全设备智能运维托管平台</p>
        </div>

        <label class="block text-xs text-slate-500 font-medium mb-1.5">用户名</label>
        <input
          v-model="username"
          type="text"
          placeholder="用户名"
          autocomplete="username"
          class="input"
        />

        <label class="block text-xs text-slate-500 font-medium mt-4 mb-1.5">密码</label>
        <input
          v-model="password"
          type="password"
          placeholder="密码"
          autocomplete="current-password"
          class="input"
        />

        <p v-if="errMsg" class="text-red-400 text-sm mt-3">{{ errMsg }}</p>

        <button
          type="submit"
          :disabled="loading"
          class="btn btn-primary w-full mt-6 py-2.5"
        >
          {{ loading ? '登录中...' : '登 录' }}
        </button>
      </form>
    </div>
  </div>
</template>
