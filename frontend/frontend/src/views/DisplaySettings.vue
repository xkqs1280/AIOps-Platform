<template>
  <div class="min-h-screen bg-app text-ink-strong animate-in">
    <!-- Header -->
    <div class="border-b border-line bg-surface/50 px-6 py-4">
      <div class="flex items-center justify-between">
        <div>
          <h1 class="text-2xl font-bold text-ink-strong">显示设置</h1>
          <p class="mt-1 text-sm text-ink-muted">切换界面明暗模式，白天、夜间观看更舒适</p>
        </div>
      </div>
    </div>

    <div class="px-6 py-6">
      <!-- 明暗模式 -->
      <div class="mb-6 rounded-xl border border-line bg-surface/50 p-5">
        <h2 class="mb-1 text-sm font-semibold text-ink-muted">明暗模式</h2>
        <p class="mb-4 text-sm text-ink-faint">
          {{ currentMode === 'system'
            ? '当前跟随系统：' + (isDark ? '系统为暗色，界面使用暗色模式' : '系统为亮色，界面使用亮色模式')
            : currentMode === 'dark'
              ? '当前固定使用暗色模式，不受系统设置影响'
              : '当前固定使用亮色模式，不受系统设置影响' }}
        </p>

        <div class="grid grid-cols-1 gap-4 md:grid-cols-3">
          <!-- 跟随系统 -->
          <button
            @click="selectMode('system')"
            class="group relative flex flex-col items-start gap-3 rounded-xl border p-4 text-left transition-all"
            :class="currentMode === 'system'
              ? 'border-cyan-500/60 bg-cyan-500/10 ring-1 ring-cyan-500/40'
              : 'border-line bg-surface hover:border-line-strong hover:bg-hover/40'"
          >
            <span
              class="flex h-10 w-10 items-center justify-center rounded-lg"
              :class="currentMode === 'system' ? 'bg-cyan-500/20 text-cyan-400' : 'bg-hover text-ink-muted group-hover:text-ink'"
            >
              <ComputerDesktopIcon class="h-5 w-5" />
            </span>
            <span class="text-sm font-semibold text-ink-strong">跟随系统</span>
            <span class="text-xs text-ink-muted">自动匹配系统明暗偏好</span>
            <span
              v-if="currentMode === 'system'"
              class="absolute right-3 top-3 flex h-5 w-5 items-center justify-center rounded-full bg-cyan-500 text-white"
            >
              <CheckIcon class="h-3.5 w-3.5" />
            </span>
          </button>

          <!-- 亮色 -->
          <button
            @click="selectMode('light')"
            class="group relative flex flex-col items-start gap-3 rounded-xl border p-4 text-left transition-all"
            :class="currentMode === 'light'
              ? 'border-amber-500/60 bg-amber-500/10 ring-1 ring-amber-500/40'
              : 'border-line bg-surface hover:border-line-strong hover:bg-hover/40'"
          >
            <span
              class="flex h-10 w-10 items-center justify-center rounded-lg"
              :class="currentMode === 'light' ? 'bg-amber-500/20 text-amber-400' : 'bg-hover text-ink-muted group-hover:text-ink'"
            >
              <SunIcon class="h-5 w-5" />
            </span>
            <span class="text-sm font-semibold text-ink-strong">亮色模式</span>
            <span class="text-xs text-ink-muted">界面始终使用亮色（白天）</span>
            <span
              v-if="currentMode === 'light'"
              class="absolute right-3 top-3 flex h-5 w-5 items-center justify-center rounded-full bg-amber-500 text-white"
            >
              <CheckIcon class="h-3.5 w-3.5" />
            </span>
          </button>

          <!-- 暗色 -->
          <button
            @click="selectMode('dark')"
            class="group relative flex flex-col items-start gap-3 rounded-xl border p-4 text-left transition-all"
            :class="currentMode === 'dark'
              ? 'border-indigo-500/60 bg-indigo-500/10 ring-1 ring-indigo-500/40'
              : 'border-line bg-surface hover:border-line-strong hover:bg-hover/40'"
          >
            <span
              class="flex h-10 w-10 items-center justify-center rounded-lg"
              :class="currentMode === 'dark' ? 'bg-indigo-500/20 text-indigo-400' : 'bg-hover text-ink-muted group-hover:text-ink'"
            >
              <MoonIcon class="h-5 w-5" />
            </span>
            <span class="text-sm font-semibold text-ink-strong">暗色模式</span>
            <span class="text-xs text-ink-muted">界面始终使用暗色（夜间）</span>
            <span
              v-if="currentMode === 'dark'"
              class="absolute right-3 top-3 flex h-5 w-5 items-center justify-center rounded-full bg-indigo-500 text-white"
            >
              <CheckIcon class="h-3.5 w-3.5" />
            </span>
          </button>
        </div>

        <p class="mt-4 text-xs text-ink-faint">
          提示：切换后页面会刷新以重建图表。侧边栏底部的太阳/月亮按钮可在亮色与暗色之间快速切换。
        </p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import { ComputerDesktopIcon, SunIcon, MoonIcon, CheckIcon } from '@heroicons/vue/24/outline'
import { getThemeMode, isDarkNow, switchTheme } from '../utils/theme.js'

const currentMode = ref(getThemeMode())
const isDark = computed(() => isDarkNow(currentMode.value))

function selectMode(mode) {
  if (mode === currentMode.value) return
  switchTheme(mode) // 内部 reload，重建 ECharts 图表
}
</script>
