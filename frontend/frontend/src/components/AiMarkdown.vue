<template>
  <div class="ai-md text-[13px] leading-relaxed break-words" v-html="html"></div>
</template>

<script setup>
// 轻量 Markdown 渲染（无外部依赖）：标题 / 粗体 / 行内代码 / 围栏代码块 / 列表 / 引用 / 分隔线。
// 全部内容先做 HTML 转义再套标签，无 XSS 风险。
import { computed } from 'vue'

const props = defineProps({ text: { type: String, default: '' } })

function esc(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, '<code class="px-1 py-0.5 rounded text-[12px] bg-hover font-mono">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
}

function renderBlock(lines) {
  const out = []
  let list = null // 'ul' | 'ol'
  const closeList = () => {
    if (list) { out.push(`</${list}>`); list = null }
  }
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, '')
    const h = line.match(/^(#{1,4})\s+(.*)$/)
    const ul = line.match(/^\s*[-*]\s+(.*)$/)
    const ol = line.match(/^\s*\d+[.、)]\s+(.*)$/)
    if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) { closeList(); out.push('<hr class="my-2 border-line" />'); continue }
    if (h) { closeList(); out.push(`<h${h[1].length + 2} class="font-bold mt-2 mb-1">${inline(h[2])}</h${h[1].length + 2}>`); continue }
    if (ul) {
      if (list !== 'ul') { closeList(); out.push('<ul class="list-disc pl-5 my-1 space-y-0.5">'); list = 'ul' }
      out.push(`<li>${inline(ul[1])}</li>`); continue
    }
    if (ol) {
      if (list !== 'ol') { closeList(); out.push('<ol class="list-decimal pl-5 my-1 space-y-0.5">'); list = 'ol' }
      out.push(`<li>${inline(ol[1])}</li>`); continue
    }
    closeList()
    if (line.trim() === '') continue
    if (/^&gt;\s?/.test(esc(line))) { out.push(`<blockquote class="border-l-2 border-line pl-2 text-ink-soft my-1">${inline(line.replace(/^\s*>\s?/, ''))}</blockquote>`); continue }
    out.push(`<p class="my-1">${inline(line)}</p>`)
  }
  closeList()
  return out.join('')
}

const html = computed(() => {
  const src = props.text || ''
  const parts = src.split(/```/)
  const frags = []
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 1) {
      // 围栏代码块：首行可能是语言标注
      const body = parts[i].replace(/^[a-zA-Z0-9_+-]*\n/, '')
      frags.push('<pre class="my-2 p-3 rounded-lg overflow-x-auto text-[12px] leading-relaxed font-mono" style="background:var(--color-code-bg,rgba(127,127,127,0.12))"><code>' + esc(body) + '</code></pre>')
    } else if (parts[i]) {
      frags.push(renderBlock(parts[i].split('\n')))
    }
  }
  return frags.join('')
})
</script>
