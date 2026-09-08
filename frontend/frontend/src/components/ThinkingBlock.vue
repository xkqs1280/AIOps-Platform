<template>
  <div v-if="text" class="thinking-block">
    <button
      type="button"
      class="w-full flex items-center gap-1.5 px-2 py-1 text-[11px] text-ink-faint hover:text-ink rounded-md transition-colors"
      @click="open = !open"
    >
      <svg v-if="!open" class="w-3 h-3 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7" /></svg>
      <svg v-else class="w-3 h-3 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M5 15l7-7 7 7" /></svg>
      <span class="font-medium">模型思考过程</span>
      <span class="opacity-60 font-mono">{{ chars }} 字</span>
      <span v-if="active" class="ml-auto flex items-center gap-1">
        <span class="inline-block w-2 h-2 rounded-full bg-cyan-500 animate-pulse"></span>
        <span class="opacity-70">思考中</span>
      </span>
    </button>
    <div v-if="open" class="px-3 pb-2">
      <div class="border-l-2 border-cyan-500/40 pl-3 text-[12px] leading-relaxed text-ink-muted whitespace-pre-wrap break-words max-h-64 overflow-y-auto thinking-scroll">
        {{ text }}<span v-if="active" class="inline-block w-[2px] h-3.5 bg-cyan-500 align-middle animate-pulse ml-0.5"></span>
      </div>
    </div>
  </div>
</template>

<script setup>
// 模型思考过程展示块：可折叠，思考中显示脉冲光标。
// props.text 非空时渲染；active=true 表示仍在思考（显示光标与呼吸点）。
import { ref, computed, watch } from 'vue'

const props = defineProps({
  text: { type: String, default: '' },
  active: { type: Boolean, default: false },
})

const open = ref(true)
const chars = computed(() => props.text.length)
watch(() => props.text, (v) => { if (v) open.value = true })
</script>

<style scoped>
.thinking-block { border-bottom: 1px solid var(--line, rgba(148, 163, 184, 0.18)); margin-bottom: 0.5rem; }
.thinking-scroll::-webkit-scrollbar { width: 4px; }
.thinking-scroll::-webkit-scrollbar-thumb { background: rgba(148, 163, 184, 0.3); border-radius: 2px; }
</style>
