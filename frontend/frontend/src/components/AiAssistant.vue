<template>
  <Teleport to="body">
    <!-- 悬浮球（可沿右侧上下拖动，位置记忆） -->
    <button
      v-if="!open"
      ref="fabRef"
      class="fixed right-6 z-40 w-12 h-12 rounded-full grad-brand text-white shadow-lg flex items-center justify-center hover:scale-105 transition-transform cursor-grab active:cursor-grabbing select-none touch-none"
      :style="fabY !== null ? { top: fabY + 'px', bottom: 'auto' } : { bottom: '24px' }"
      title="AI 运维助手（可上下拖动）"
      @pointerdown="onFabDown"
      @pointermove="onFabMove"
      @pointerup="onFabUp"
    >
      <SparklesIcon class="w-6 h-6 pointer-events-none" />
    </button>

    <!-- 侧边抽屉 -->
    <div v-if="open" class="fixed inset-0 z-50">
      <div class="absolute inset-0 bg-black/40" @click="open = false"></div>
      <div class="absolute right-0 top-0 h-full w-full sm:w-[440px] bg-surface border-l border-line flex flex-col shadow-2xl">
        <div class="flex items-center justify-between px-5 py-3.5 border-b border-line shrink-0">
          <h3 class="text-[15px] font-bold flex items-center gap-2">
            <SparklesIcon class="w-5 h-5 text-cyan-500" />AI 运维助手
          </h3>
          <div class="flex items-center gap-1">
            <button @click="clearChat" class="px-2.5 py-1.5 text-[12px] rounded-lg text-ink-faint hover:text-ink hover:bg-hover">清空对话</button>
            <button @click="open = false" class="p-2 rounded-lg text-ink-faint hover:text-ink hover:bg-hover" title="关闭">
              <XMarkIcon class="w-[18px] h-[18px]" />
            </button>
          </div>
        </div>

        <div ref="listRef" class="flex-1 overflow-y-auto px-4 py-4 space-y-3">
          <div v-if="messages.length === 0" class="text-center text-ink-faint text-[13px] py-10 space-y-2">
            <SparklesIcon class="w-8 h-8 mx-auto opacity-50" />
            <p>问我任何运维问题，例如：</p>
            <div class="space-y-1.5">
              <button v-for="q in presets" :key="q" @click="ask(q)"
                class="block mx-auto px-3 py-1.5 rounded-lg bg-hover hover:bg-line text-[12px] transition-colors">{{ q }}</button>
            </div>
          </div>

          <div v-for="(m, i) in messages" :key="i" :class="m.role === 'user' ? 'flex justify-end' : 'flex justify-start'">
            <div
              class="max-w-[85%] px-3.5 py-2.5 rounded-2xl text-[13px]"
              :class="m.role === 'user'
                ? 'bg-cyan-600 text-white rounded-br-md'
                : 'bg-hover text-ink rounded-bl-md'"
            >
              <AiMarkdown v-if="m.role === 'assistant'" :text="m.content + (m.loading ? ' ▍' : '')" />
              <template v-else>{{ m.content }}</template>
              <div v-if="m.error" class="text-red-400 text-[12px] mt-1">{{ m.error }}</div>
            </div>
          </div>
        </div>

        <div class="border-t border-line p-3 shrink-0">
          <div class="flex items-end gap-2">
            <textarea
              v-model="input"
              rows="2"
              placeholder="输入问题，Enter 发送，Shift+Enter 换行"
              class="flex-1 resize-none px-3 py-2 rounded-xl bg-hover border border-line text-[13px] outline-none focus:border-cyan-500"
              :disabled="streaming"
              @keydown.enter.exact.prevent="send"
            ></textarea>
            <button
              @click="send"
              :disabled="streaming || !input.trim()"
              class="px-3.5 py-2.5 rounded-xl grad-brand text-white text-[13px] font-medium disabled:opacity-40 shrink-0"
            >发送</button>
          </div>
          <p class="text-[10px] text-ink-faint mt-1.5 px-1">AI 生成内容仅供参考，命令需人工确认后执行</p>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
// 全局 AI 助手抽屉：多轮对话（内存态），SSE 流式输出。
import { ref, nextTick } from 'vue'
import { SparklesIcon, XMarkIcon } from '@heroicons/vue/24/outline'
import { aiStream } from '../api/ai.js'
import AiMarkdown from './AiMarkdown.vue'

const open = ref(false)
const input = ref('')
const streaming = ref(false)
const listRef = ref(null)
const messages = ref([]) // {role, content, loading?, error?}

const presets = [
  'CPU 告警的常见排查步骤是什么？',
  'H3C 交换机如何查看端口错包？',
  '如何配置设备定时备份？',
]

// ---- 悬浮球拖拽（仅垂直方向，位置记忆） ----
const fabRef = ref(null)
const FAB_H = 48
const fabY = ref((() => {
  const v = Number(localStorage.getItem('aiops_fab_y'))
  return Number.isFinite(v) && v > 0 ? v : null
})())
let drag = null // { startY, top, moved }

function clampY(top) {
  return Math.min(Math.max(8, top), window.innerHeight - FAB_H - 8)
}

function onFabDown(e) {
  const rect = fabRef.value.getBoundingClientRect()
  drag = { startY: e.clientY, top: rect.top, moved: false }
  fabRef.value.setPointerCapture(e.pointerId)
}

function onFabMove(e) {
  if (!drag) return
  const dy = e.clientY - drag.startY
  if (Math.abs(dy) > 4) drag.moved = true
  if (!drag.moved) return
  fabY.value = clampY(drag.top + dy)
}

function onFabUp() {
  if (!drag) return
  if (drag.moved) {
    localStorage.setItem('aiops_fab_y', String(Math.round(fabY.value)))
  } else {
    open.value = true
  }
  drag = null
}

// 窗口尺寸变化时防止悬浮球越界
window.addEventListener('resize', () => {
  if (fabY.value !== null) fabY.value = clampY(fabY.value)
})

function scrollBottom() {
  nextTick(() => { if (listRef.value) listRef.value.scrollTop = listRef.value.scrollHeight })
}

function clearChat() {
  if (streaming.value) return
  messages.value = []
}

function ask(q) {
  input.value = q
  send()
}

function send() {
  const text = input.value.trim()
  if (!text || streaming.value) return
  input.value = ''
  streaming.value = true
  messages.value.push({ role: 'user', content: text })
  const reply = { role: 'assistant', content: '', loading: true, error: '' }
  messages.value.push(reply)
  scrollBottom()

  const history = messages.value
    .filter((m) => !m.error)
    .slice(0, -1)
    .slice(-12)
    .map((m) => ({ role: m.role, content: m.content }))

  aiStream('/ai/chat', { messages: history }, {
    onDelta(t) {
      reply.content += t
      scrollBottom()
    },
    onError(e) {
      reply.error = e
      reply.loading = false
      streaming.value = false
      scrollBottom()
    },
    onDone() {
      reply.loading = false
      streaming.value = false
      scrollBottom()
    },
  })
}
</script>
