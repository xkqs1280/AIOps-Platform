import { createApp } from 'vue'
import App from './App.vue'
import router from './router.js'
import api, { setUnauthorizedHandler } from './api.js'
import './style.css'

// 会话过期（任意接口 401）→ 回登录页；登录失败由 api.js 端点过滤，不会走到这里
setUnauthorizedHandler(() => {
  if (!window.location.hash.startsWith('#/login')) {
    window.location.hash = '#/login'
  }
})

const app = createApp(App)
app.use(router)
app.mount('#app')
