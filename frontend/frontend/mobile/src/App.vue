<template>
  <div
    class="flex h-full flex-col bg-app text-ink-strong"
    style="padding-top: env(safe-area-inset-top)"
  >
    <!-- 内容区 -->
    <main class="flex-1 overflow-y-auto pb-16">
      <router-view />
    </main>

    <!-- 底部 Tab（登录页不显示） -->
    <nav
      v-if="route.path !== '/login'"
      class="fixed bottom-0 left-0 right-0 z-40 border-t border-line bg-surface/95 backdrop-blur"
      style="padding-bottom: env(safe-area-inset-bottom)"
    >
      <div class="flex">
        <router-link
          v-for="t in tabs"
          :key="t.path"
          :to="t.path"
          class="flex flex-1 flex-col items-center gap-0.5 py-2 text-[10px]"
          :class="isActive(t.path) ? 'text-cyan-400' : 'text-ink-faint'"
        >
          <span class="text-xl leading-none">{{ t.icon }}</span>
          <span>{{ t.label }}</span>
        </router-link>
      </div>
    </nav>
  </div>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { startAlertPolling } from './notifications.js'

const route = useRoute()

const tabs = [
  { path: '/', label: '监控', icon: '📊' },
  { path: '/devices', label: '设备', icon: '🖥' },
  { path: '/alerts', label: '告警', icon: '🔔' },
  { path: '/topology', label: '拓扑', icon: '🕸' },
  { path: '/more', label: '更多', icon: '☰' },
]

const isActive = (path) => {
  if (path === '/') return route.path === '/'
  return route.path.startsWith(path)
}

// 未登录跳登录页（本地无 token）
const hasToken = computed(() => !!localStorage.getItem('aiops_mobile_token'))
if (!hasToken.value && route.path !== '/login') {
  window.location.hash = '#/login'
}

// APP 启动时若已登录（有 token），启动告警轮询
onMounted(() => {
  if (hasToken.value) startAlertPolling()
})
</script>
