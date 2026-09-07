<template>
  <Teleport to="body">
    <div v-if="open" class="fixed inset-0 z-50 flex flex-col justify-end">
      <!-- 遮罩 -->
      <div class="absolute inset-0 bg-black/50" @click="close"></div>

      <!-- 底部弹层 -->
      <div class="relative mx-2 mb-2 max-h-[78vh] flex flex-col rounded-2xl border border-line bg-surface shadow-2xl">
        <!-- 标题栏 -->
        <div class="flex items-center gap-2 border-b border-line px-4 py-3">
          <span class="text-sm font-semibold text-ink-strong">{{ title }}</span>
          <span v-if="cached" class="rounded bg-cyan-500/15 px-1.5 py-0.5 text-[10px] text-cyan-400">缓存</span>
          <div class="ml-auto flex gap-2">
            <button @click="run" :disabled="loading" class="rounded-lg border border-line px-2.5 py-1 text-xs text-ink-muted disabled:opacity-40">重试</button>
            <button @click="close" class="rounded-lg border border-line px-2.5 py-1 text-xs text-ink-muted">关闭</button>
          </div>
        </div>

        <!-- 内容区 -->
        <div ref="bodyRef" class="flex-1 overflow-y-auto px-4 py-3">
          <div v-if="error" class="rounded-lg border border-red-800/40 bg-red-900/20 px-3 py-2 text-xs text-red-400">{{ error }}</div>
          <div v-else-if="!content && loading" class="flex items-center gap-2 py-6 text-xs text-ink-faint">
            <span class="h-2 w-2 animate-pulse rounded-full bg-cyan-400"></span> AI 正在生成…
          </div>
          <AiMarkdown v-else :text="content" />
        </div>

        <p class="border-t border-line px-4 py-2 text-[10px] text-ink-faint">AI 生成内容仅供参考，操作前请人工确认</p>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { ref, watch, nextTick } from 'vue'
import AiMarkdown from './AiMarkdown.vue'
import { aiStream } from '../ai.js'

const props = defineProps({
  open: { type: Boolean, default: false },
  title: { type: String, default: 'AI 解读' },
  path: { type: String, required: true },
  body: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['update:open'])

const content = ref('')
const loading = ref(false)
const error = ref('')
const cached = ref(false)
const bodyRef = ref(null)
let seq = 0

function scroll() {
  nextTick(() => {
    const el = bodyRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function run() {
  const my = ++seq
  content.value = ''
  error.value = ''
  cached.value = false
  loading.value = true
  aiStream(props.path, props.body, {
    onDelta(t) {
      if (my !== seq) return
      content.value += t
      scroll()
    },
    onCached() { if (my === seq) cached.value = true },
    onError(e) { if (my === seq) error.value = e },
    onDone() { if (my === seq) loading.value = false },
  })
}

function close() {
  seq++ // 中止旧流写入
  loading.value = false
  emit('update:open', false)
}

watch(() => props.open, (v) => { if (v) run() })
</script>
