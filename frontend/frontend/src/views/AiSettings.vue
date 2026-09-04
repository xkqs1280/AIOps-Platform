<template>
  <div class="p-6 max-w-3xl mx-auto animate-in">
    <div class="flex items-center justify-between mb-6">
      <div>
        <h2 class="text-xl font-bold text-ink-strong">AI 辅助设置</h2>
        <p class="text-sm text-ink-faint mt-1">接入大模型：告警解读、运维问答、配置差异分析、巡检总结与知识库检索</p>
      </div>
      <span v-if="isAdmin" class="px-2 py-0.5 rounded text-xs bg-cyan-500/10 text-cyan-400 border border-cyan-500/30">管理员</span>
    </div>

    <div v-if="!isAdmin" class="bg-surface border border-line rounded-xl p-8 text-center">
      <p class="text-sm text-ink-muted">仅管理员可配置 AI 接入与知识库</p>
    </div>

    <div v-else class="space-y-6">
      <!-- 接入配置 -->
      <div class="bg-surface border border-line rounded-xl p-6 space-y-5">
        <div class="flex items-center justify-between">
          <div>
            <div class="text-sm font-medium text-ink">启用 AI 功能</div>
            <div class="text-xs text-ink-faint mt-1">开启后告警解读 / AI 助手 / 差异分析等入口生效</div>
          </div>
          <button
            @click="cfg.enabled = !cfg.enabled"
            class="relative w-11 h-6 rounded-full transition-colors shrink-0"
            :class="cfg.enabled ? 'bg-cyan-600' : 'bg-hover'"
          >
            <span class="absolute top-0.5 w-5 h-5 rounded-full bg-white transition-all" :class="cfg.enabled ? 'left-[22px]' : 'left-0.5'" />
          </button>
        </div>

        <div class="border-t border-line" />

        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label class="text-xs text-ink-faint font-medium block mb-1.5">接入方式</label>
            <select v-model="cfg.provider" class="w-full bg-surface-2 border border-line rounded-lg px-3 py-2 text-sm text-ink focus:border-cyan-500 focus:outline-none">
              <option value="ollama">Ollama 本地（离线首选）</option>
              <option value="gateway">内网推理网关（vLLM 等）</option>
              <option value="cloud">云端 API（DeepSeek 等）</option>
            </select>
          </div>
          <div>
            <label class="text-xs text-ink-faint font-medium block mb-1.5">服务地址（OpenAI 兼容）</label>
            <input v-model="cfg.base_url" placeholder="http://127.0.0.1:11434/v1" class="w-full bg-surface-2 border border-line rounded-lg px-3 py-2 text-sm text-ink focus:border-cyan-500 focus:outline-none" />
          </div>
          <div>
            <label class="text-xs text-ink-faint font-medium block mb-1.5">对话模型</label>
            <input v-model="cfg.model" placeholder="qwen2.5:7b-instruct" class="w-full bg-surface-2 border border-line rounded-lg px-3 py-2 text-sm text-ink focus:border-cyan-500 focus:outline-none" />
          </div>
          <div>
            <label class="text-xs text-ink-faint font-medium block mb-1.5">向量模型（知识库用）</label>
            <input v-model="cfg.embed_model" placeholder="nomic-embed-text" class="w-full bg-surface-2 border border-line rounded-lg px-3 py-2 text-sm text-ink focus:border-cyan-500 focus:outline-none" />
          </div>
          <div>
            <label class="text-xs text-ink-faint font-medium block mb-1.5">API Key（云端接入时填写）</label>
            <input v-model="cfg.api_key" type="password" placeholder="留空则不修改" class="w-full bg-surface-2 border border-line rounded-lg px-3 py-2 text-sm text-ink focus:border-cyan-500 focus:outline-none" />
          </div>
          <div>
            <label class="text-xs text-ink-faint font-medium block mb-1.5">生成温度 {{ cfg.temperature.toFixed(1) }}</label>
            <input v-model.number="cfg.temperature" type="range" min="0" max="1" step="0.1" class="w-full accent-cyan-500 mt-3" />
          </div>
        </div>

        <p class="text-[11px] text-ink-faint">本地 Ollama 需设置环境变量 OLLAMA_HOST=0.0.0.0 后重启，供远程平台访问；云端 API 数据将出网，请确认合规后再启用。</p>

        <div class="flex items-center gap-3">
          <button @click="save" :disabled="saving" class="px-4 py-2 rounded-lg grad-brand text-white text-sm font-medium disabled:opacity-50">
            {{ saving ? '保存中…' : '保存配置' }}
          </button>
          <button @click="test" :disabled="testing" class="px-4 py-2 rounded-lg border border-line text-sm text-ink hover:bg-hover disabled:opacity-50">
            {{ testing ? '测试中…' : '测试连接' }}
          </button>
          <span v-if="msg" :class="msgOk ? 'text-green-400' : 'text-red-400'" class="text-xs">{{ msg }}</span>
        </div>
        <div v-if="testResult" class="text-xs px-3 py-2 rounded-lg" :class="testResult.ok ? 'bg-green-500/10 text-green-400' : 'bg-red-500/10 text-red-400'">
          {{ testResult.ok ? `连接成功，延迟 ${testResult.latency_ms}ms，模型回复：${testResult.reply || 'OK'}` : `连接失败：${testResult.error}` }}
        </div>
      </div>

      <!-- 知识库 -->
      <div class="bg-surface border border-line rounded-xl p-6 space-y-4">
        <div>
          <div class="text-sm font-medium text-ink">运维知识库（RAG）</div>
          <div class="text-xs text-ink-faint mt-1">上传厂商手册、拓扑说明等文档（txt/md/log/csv/conf/cfg，≤2MB），AI 助手问答时自动检索引用</div>
        </div>

        <div class="flex items-center gap-3">
          <label class="px-4 py-2 rounded-lg border border-line text-sm text-ink hover:bg-hover cursor-pointer">
            {{ uploading ? '解析中…' : '上传文档' }}
            <input type="file" class="hidden" accept=".txt,.md,.log,.csv,.conf,.cfg" :disabled="uploading" @change="onUpload" />
          </label>
          <input v-model="kbNote" placeholder="备注（可选）" class="flex-1 bg-surface-2 border border-line rounded-lg px-3 py-2 text-sm text-ink focus:border-cyan-500 focus:outline-none" />
        </div>

        <div class="border rounded-lg border-line divide-y divide-line max-h-56 overflow-y-auto">
          <div v-if="kbDocs.length === 0" class="px-3 py-6 text-center text-xs text-ink-faint">暂无文档</div>
          <div v-for="d in kbDocs" :key="d.id" class="flex items-center justify-between px-3 py-2 text-sm">
            <div class="min-w-0">
              <div class="truncate text-ink">{{ d.filename }}</div>
              <div class="text-[11px] text-ink-faint">{{ d.chunk_count }} 块 · {{ (d.size / 1024).toFixed(1) }}KB · {{ fmtTime(d.created_at) }}{{ d.note ? ' · ' + d.note : '' }}</div>
            </div>
            <div class="flex items-center gap-2 shrink-0 ml-3">
              <span class="text-[11px]" :class="d.status === 'ready' ? 'text-green-400' : d.status === 'failed' ? 'text-red-400' : 'text-amber-400'">
                {{ d.status === 'ready' ? '就绪' : d.status === 'failed' ? '失败' : '处理中' }}
              </span>
              <button @click="delDoc(d.id)" class="text-red-400 hover:text-red-300 text-xs">删除</button>
            </div>
          </div>
        </div>

        <div class="flex items-center gap-2">
          <input v-model="kbQuery" placeholder="检索测试：输入关键词，查看命中片段" class="flex-1 bg-surface-2 border border-line rounded-lg px-3 py-2 text-sm text-ink focus:border-cyan-500 focus:outline-none" @keydown.enter="doSearch" />
          <button @click="doSearch" class="px-3 py-2 rounded-lg border border-line text-sm text-ink hover:bg-hover shrink-0">检索</button>
        </div>
        <div v-if="searchHits.length" class="space-y-2">
          <div v-for="(h, i) in searchHits" :key="i" class="text-xs bg-hover rounded-lg px-3 py-2">
            <span class="text-cyan-400 font-medium">{{ h.filename }}</span>
            <span class="text-ink-faint ml-2">相关度 {{ (h.score * 100).toFixed(0) }}%</span>
            <p class="mt-1 text-ink-muted line-clamp-3">{{ h.content.slice(0, 160) }}</p>
          </div>
        </div>
      </div>

      <!-- 调用审计 -->
      <div class="bg-surface border border-line rounded-xl p-6 space-y-3">
        <div class="flex items-center justify-between">
          <div class="text-sm font-medium text-ink">AI 调用审计</div>
          <span class="text-xs text-ink-faint">共 {{ total }} 次</span>
        </div>
        <div class="border rounded-lg border-line divide-y divide-line max-h-72 overflow-y-auto">
          <div v-if="logs.length === 0" class="px-3 py-6 text-center text-xs text-ink-faint">暂无调用记录</div>
          <div v-for="l in logs" :key="l.id" class="flex items-center justify-between px-3 py-2 text-xs">
            <div>
              <span class="text-ink">{{ sceneName(l.scene) }}</span>
              <span class="text-ink-faint ml-2">{{ l.user }}</span>
              <span v-if="l.target" class="text-ink-faint ml-1">· {{ l.target }}</span>
            </div>
            <div class="flex items-center gap-3 shrink-0">
              <span :class="l.ok ? 'text-green-400' : 'text-red-400'">{{ l.ok ? `${l.duration_ms}ms` : '失败' }}</span>
              <span class="text-ink-faint">{{ fmtTime(l.created_at) }}</span>
            </div>
          </div>
        </div>
        <button v-if="logs.length < total" @click="loadLogs" class="text-xs text-cyan-400 hover:text-cyan-300">加载更多</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getMe } from '../api/index.js'
import { getAiConfig, saveAiConfig, testAiConnection, aiKbDocs, aiKbUpload, aiKbDelete, aiKbSearch, aiLogs } from '../api/ai.js'

const isAdmin = ref(false)
const cfg = ref({ enabled: false, provider: 'ollama', base_url: '', model: '', api_key: '', temperature: 0.3, embed_model: '' })
const saving = ref(false)
const testing = ref(false)
const msg = ref('')
const msgOk = ref(false)
const testResult = ref(null)

const kbDocs = ref([])
const kbNote = ref('')
const uploading = ref(false)
const kbQuery = ref('')
const searchHits = ref([])

const logs = ref([])
const total = ref(0)
const page = ref(0)

const SCENES = { chat: 'AI 助手', alert: '告警解读', backup: '配置差异', inspection: '巡检总结', cli: '命令助手', report: '运维日报' }
const sceneName = (s) => SCENES[s] || s
const fmtTime = (t) => (t ? new Date(t).toLocaleString('zh-CN', { hour12: false }) : '-')

async function load() {
  const data = await getAiConfig()
  const d = data.data || data
  cfg.value = { ...cfg.value, ...d, api_key: '' }
  const docs = await aiKbDocs()
  kbDocs.value = (docs.data || docs).items || []
  loadLogs()
}

async function loadLogs() {
  const next = page.value + 1
  const data = await aiLogs({ page: next, page_size: 20 })
  const d = data.data || data
  total.value = d.total
  logs.value = next === 1 ? d.items : logs.value.concat(d.items)
  page.value = next
}

async function save() {
  saving.value = true
  msg.value = ''
  try {
    await saveAiConfig(cfg.value)
    msgOk.value = true
    msg.value = 'AI 配置已保存'
    setTimeout(() => (msg.value = ''), 3000)
  } catch (e) {
    msgOk.value = false
    msg.value = '保存失败：' + (e.response?.data?.detail || e.message || '未知错误')
  } finally {
    saving.value = false
  }
}

async function test() {
  testing.value = true
  testResult.value = null
  try {
    const data = await testAiConnection()
    testResult.value = data.data || data
  } catch (e) {
    testResult.value = { ok: false, error: e.response?.data?.detail || e.message || '请求失败' }
  } finally {
    testing.value = false
  }
}

async function onUpload(ev) {
  const file = ev.target.files[0]
  ev.target.value = ''
  if (!file) return
  uploading.value = true
  try {
    const fd = new FormData()
    fd.append('file', file)
    if (kbNote.value) fd.append('note', kbNote.value)
    await aiKbUpload(fd)
    kbNote.value = ''
    const docs = await aiKbDocs()
    kbDocs.value = (docs.data || docs).items || []
  } catch (e) {
    alert('上传失败：' + (e.response?.data?.detail || e.message || '未知错误'))
  } finally {
    uploading.value = false
  }
}

async function delDoc(id) {
  if (!confirm('确认删除该文档及其知识块？')) return
  await aiKbDelete(id)
  kbDocs.value = kbDocs.value.filter((d) => d.id !== id)
}

async function doSearch() {
  if (!kbQuery.value.trim()) return
  const data = await aiKbSearch(kbQuery.value)
  searchHits.value = (data.data || data).items || []
}

onMounted(async () => {
  try {
    const me = await getMe()
    isAdmin.value = me.data?.role === 'admin' || me.role === 'admin'
    if (isAdmin.value) await load()
  } catch { /* 静默 */ }
})
</script>
