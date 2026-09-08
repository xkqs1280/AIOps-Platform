<template>
  <Teleport to="body">
    <div v-if="open" class="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div class="absolute inset-0 bg-black/50 backdrop-blur-sm" @click="close"></div>
      <div class="relative w-full max-w-2xl max-h-[82vh] flex flex-col bg-surface border border-line rounded-2xl shadow-2xl">
        <div class="flex items-center justify-between px-5 py-3.5 border-b border-line shrink-0">
          <h3 class="text-[15px] font-bold flex items-center gap-2">
            <span class="w-1.5 h-4 rounded-full grad-brand inline-block"></span>{{ title }}
            <span v-if="cached" class="text-[10px] px-1.5 py-0.5 rounded bg-hover text-ink-faint">缓存</span>
          </h3>
          <div class="flex items-center gap-1">
            <button v-if="content && !loading" @click="copy" class="p-2 rounded-lg text-ink-faint hover:text-ink hover:bg-hover" title="复制">
              <ClipboardDocumentIcon class="w-4.5 h-4.5 w-[18px] h-[18px]" />
            </button>
            <button v-if="!loading && error" @click="run" class="p-2 rounded-lg text-ink-faint hover:text-ink hover:bg-hover" title="重试">
              <ArrowPathIcon class="w-[18px] h-[18px]" />
            </button>
            <button @click="close" class="p-2 rounded-lg text-ink-faint hover:text-ink hover:bg-hover" title="关闭">
              <XMarkIcon class="w-[18px] h-[18px]" />
            </button>
          </div>
        </div>
        <div ref="bodyRef" class="flex-1 overflow-y-auto px-5 py-4">
          <div v-if="thinking" class="mb-3">
            <div class="flex items-center gap-2 text-ink-faint text-[13px] mb-1.5">
              <span v-if="loading && !content" class="inline-block w-3 h-3 border-2 border-current border-t-transparent rounded-full animate-spin"></span>
              <template v-if="loading && !content">AI 思考中…</template>
              <template v-else>思考过程</template>
            </div>
            <div class="rounded-xl border border-line-strong/60 bg-hover/60 p-3 max-h-56 overflow-y-auto whitespace-pre-wrap break-words text-[12px] leading-relaxed text-ink-muted font-mono">
              {{ thinking }}<span v-if="loading && !content" class="inline-block w-[2px] h-3 bg-cyan-500 align-middle animate-pulse ml-0.5"></span>
            </div>
          </div>
          <div v-else-if="!content && loading" class="flex items-center gap-2 text-ink-faint text-[13px] py-8 justify-center">
            <span class="inline-block w-3.5 h-3.5 border-2 border-current border-t-transparent rounded-full animate-spin"></span>
            AI 正在思考…
          </div>
          <div v-else-if="error && !content" class="text-danger text-[13px] py-6 text-center">{{ error }}</div>
          <AiMarkdown v-if="content" :text="content" />
          <p v-if="content" class="mt-3 text-[11px] text-ink-faint border-t border-line pt-2">
            AI 生成内容仅供参考，命令与配置需人工确认后执行。
          </p>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
// 通用 AI 解读面板：传入 path（如 /ai/explain/alert/1）与 body，打开即流式渲染。
import { ref, watch, nextTick } from 'vue'
import { XMarkIcon, ClipboardDocumentIcon, ArrowPathIcon } from '@heroicons/vue/24/outline'
import { aiStream } from '../api/ai.js'
import AiMarkdown from './AiMarkdown.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  title: { type: String, default: 'AI 解读' },
  path: { type: String, required: true },
  body: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['update:open'])

const content = ref('')
const thinking = ref('')
const loading = ref(false)
const error = ref('')
const cached = ref(false)
const bodyRef = ref(null)
let seq = 0 // 防止快速关闭后旧流继续写入

function close() { emit('update:open', false) }

async function copy() {
  try { await navigator.clipboard.writeText(content.value) } catch { /* 忽略 */ }
}

async function run() {
  const my = ++seq
  content.value = ''
  thinking.value = ''
  error.value = ''
  cached.value = false
  loading.value = true
  await aiStream(props.path, props.body, {
    onReasoning(r) {
      if (my !== seq) return
      thinking.value += r
      nextTick(() => { if (bodyRef.value) bodyRef.value.scrollTop = bodyRef.value.scrollHeight })
    },
    onDelta(t) {
      if (my !== seq) return
      content.value += t
      nextTick(() => { if (bodyRef.value) bodyRef.value.scrollTop = bodyRef.value.scrollHeight })
    },
    onCached() { if (my === seq) cached.value = true },
    onError(e) { if (my === seq) error.value = e },
    onDone() { if (my === seq) loading.value = false },
  })
  if (my === seq) loading.value = false
}

watch(() => props.open, (v) => { if (v) run() })
</script>
