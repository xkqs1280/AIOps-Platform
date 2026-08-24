import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import './style.css'

// 全局 fetch 包装：统一超时 + 401 跳登录（覆盖各页面直接使用的原生 fetch）
const _origFetch = window.fetch
window.fetch = async (input, init) => {
  const controller = new AbortController()
  const timeout = (init && init.timeout) || 15000
  const timer = setTimeout(() => controller.abort(), timeout)
  try {
    const res = await _origFetch(input, { ...(init || {}), signal: controller.signal })
    if (res.status === 401 && !window.location.pathname.startsWith('/login')) {
      // 会话失效：跳转登录页（避免重复跳转）
      if (!window.__loginRedirecting) {
        window.__loginRedirecting = true
        window.location.href = '/login'
      }
    }
    return res
  } finally {
    clearTimeout(timer)
  }
}

const app = createApp(App)
app.use(router)
app.mount('#app')
