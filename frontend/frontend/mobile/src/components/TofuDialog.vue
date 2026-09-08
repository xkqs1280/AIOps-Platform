<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="fixed inset-0 z-[60] flex items-end justify-center bg-black/60"
      @click.self="cancel"
    >
      <div class="max-h-[80vh] w-full max-w-md overflow-y-auto rounded-t-2xl border-t border-line bg-surface p-5 shadow-2xl">
        <!-- 确认模式：首次连接 / 证书变更 -->
        <template v-if="mode === 'confirm'">
          <div class="flex items-start justify-between">
            <div>
              <h2 class="text-base font-bold text-ink-strong">
                {{ isFirst ? '首次连接 · 确认服务器身份' : '服务器证书指纹已变化' }}
              </h2>
              <p class="mt-0.5 font-mono text-xs text-cyan-400">{{ displayHost }}</p>
            </div>
            <button class="rounded-lg px-2 py-1 text-lg text-ink-faint" @click="cancel">✕</button>
          </div>

          <p class="mt-3 rounded-xl border border-line bg-surface-2 p-3 text-xs leading-relaxed text-ink-muted">
            <template v-if="isFirst">
              这是首次连接该 AIOps 平台。请核对下方证书指纹与平台管理员提供的一致后，点击「信任」，之后将自动免证书连接。
            </template>
            <template v-else>
              该平台证书与之前信任的不一致，可能是服务器更换了证书，也可能是连接被劫持。请与管理员核实后再操作。
            </template>
          </p>

          <template v-if="!isFirst">
            <p class="mt-3 text-[11px] text-ink-faint">已信任的指纹</p>
            <div class="mt-1 flex items-center gap-2 rounded-xl border border-red-500/40 bg-black/40 px-3 py-2">
              <code class="min-w-0 flex-1 break-all font-mono text-[11px] leading-relaxed text-emerald-300">{{
                info.pinnedFingerprint || '—'
              }}</code>
              <button
                v-if="info.pinnedFingerprint"
                class="shrink-0 text-[11px] text-ink-faint underline underline-offset-2"
                @click="copyFp(info.pinnedFingerprint)"
              >
                复制
              </button>
            </div>
          </template>

          <p class="mt-3 text-[11px] text-ink-faint">服务器当前证书指纹（SHA-256）</p>
          <div class="mt-1 flex items-center gap-2 rounded-xl border border-line bg-black/40 px-3 py-2">
            <code class="min-w-0 flex-1 break-all font-mono text-[11px] leading-relaxed text-emerald-300">{{
              info.fingerprint || '—'
            }}</code>
            <button
              v-if="info.fingerprint"
              class="shrink-0 text-[11px] text-ink-faint underline underline-offset-2"
              @click="copyFp(info.fingerprint)"
            >
              复制
            </button>
          </div>

          <div v-if="cn || notAfterText" class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-ink-faint">
            <span v-if="cn">证书 CN：{{ cn }}</span>
            <span v-if="notAfterText">有效期至：{{ notAfterText }}</span>
          </div>

          <div class="mt-5 flex gap-3">
            <button class="flex-1 rounded-xl border border-line py-3 text-sm text-ink-muted" :disabled="busy" @click="cancel">
              取消
            </button>
            <button
              class="flex-1 rounded-xl bg-cyan-600 py-3 text-sm font-semibold text-white disabled:opacity-50"
              :disabled="busy"
              @click="trust"
            >
              {{ busy ? '处理中…' : '信任并继续' }}
            </button>
          </div>
        </template>

        <!-- 管理模式：已信任平台列表 -->
        <template v-else>
          <div class="flex items-start justify-between">
            <h2 class="text-base font-bold text-ink-strong">已信任的平台</h2>
            <button class="rounded-lg px-2 py-1 text-lg text-ink-faint" @click="closeManage">✕</button>
          </div>
          <p class="mt-1 text-[11px] text-ink-faint">
            首次连接确认后自动记录；若平台更换证书需先删除旧记录再重新连接。
          </p>

          <div
            v-if="pins.length === 0"
            class="mt-4 rounded-xl border border-dashed border-line p-6 text-center text-xs text-ink-faint"
          >
            暂无已信任的平台
          </div>
          <div v-else class="mt-3 space-y-2">
            <div v-for="p in pins" :key="p.host" class="flex items-center gap-2 rounded-xl border border-line bg-surface-2 px-3 py-2">
              <div class="min-w-0 flex-1">
                <p class="truncate font-mono text-xs text-ink-strong">{{ p.host }}</p>
                <p class="truncate font-mono text-[10px] text-ink-faint">{{ p.fingerprint }}</p>
              </div>
              <button class="shrink-0 rounded-lg border border-line px-2 py-1 text-[11px] text-red-400" @click="removePin(p.host)">
                删除
              </button>
            </div>
          </div>

          <button class="mt-5 w-full rounded-xl border border-line py-3 text-sm text-ink-muted" @click="closeManage">
            完成
          </button>
        </template>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { registerTofuHandlers, unregisterTofuHandlers, pinServer, unpinServer, listPins } from '../tofu.js'

const visible = ref(false)
const mode = ref('confirm') // confirm | manage
const info = ref({})
const busy = ref(false)
const pins = ref([])

let pendingResolve = null

const isFirst = computed(() => info.value.code === 'TOFU_FIRST_USE')
const displayHost = computed(() => `${info.value.host || ''}:${info.value.port || ''}`)
const cn = computed(() => {
  const subject = info.value.subject || ''
  const m = subject.match(/(?:^|,)\s*CN=([^,]+)/i)
  return m ? m[1].trim() : ''
})
const notAfterText = computed(() => fmtDate(info.value.notAfter))

function fmtDate(ms) {
  if (!ms) return ''
  const d = new Date(ms)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

async function copyFp(fp) {
  try {
    await navigator.clipboard.writeText(fp)
  } catch (e) {
    /* 剪贴板不可用时忽略 */
  }
}

function openConfirm(evt) {
  info.value = evt
  mode.value = 'confirm'
  busy.value = false
  visible.value = true
  return new Promise((resolve) => {
    pendingResolve = resolve
  })
}

async function trust() {
  if (busy.value) return
  busy.value = true
  try {
    await pinServer(info.value.host, info.value.port, info.value.fingerprint)
    visible.value = false
    if (pendingResolve) {
      pendingResolve(true)
      pendingResolve = null
    }
  } catch (e) {
    visible.value = false
    if (pendingResolve) {
      pendingResolve(false)
      pendingResolve = null
    }
  } finally {
    busy.value = false
  }
}

function cancel() {
  visible.value = false
  if (pendingResolve) {
    pendingResolve(false)
    pendingResolve = null
  }
}

async function openManage() {
  mode.value = 'manage'
  visible.value = true
  await refreshPins()
}
function closeManage() {
  visible.value = false
}

async function refreshPins() {
  try {
    pins.value = await listPins()
  } catch (e) {
    pins.value = []
  }
}

async function removePin(host) {
  const idx = host.lastIndexOf(':')
  const h = idx > 0 ? host.slice(0, idx) : host
  const p = idx > 0 ? Number(host.slice(idx + 1)) || 443 : 443
  try {
    await unpinServer(h, p)
  } catch (e) {
    /* ignore */
  }
  await refreshPins()
}

onMounted(() => {
  registerTofuHandlers({
    confirm: (evt) => openConfirm(evt),
    manage: () => openManage(),
  })
})
onUnmounted(() => {
  unregisterTofuHandlers()
})
</script>
