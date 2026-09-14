import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import './style.css'
import { installFetchGuard } from './utils/netGuard'

// 全局 fetch 包装：统一超时（按路径区分长短）+ 401 跳登录。
// 超时策略与「为什么设备类接口要放宽到分钟级」见 utils/netGuard.js 顶部说明。
installFetchGuard(window)

const app = createApp(App)
app.use(router)
app.mount('#app')
