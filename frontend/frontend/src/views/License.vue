<template>
  <div class="p-6 max-w-3xl mx-auto animate-in">
    <div class="flex items-center justify-between mb-6">
      <div>
        <h2 class="text-xl font-bold text-slate-100">授权管理</h2>
        <p class="text-sm text-slate-500 mt-1">平台版本授权与激活（离线激活码，绑定本机）</p>
      </div>
    </div>

    <!-- 状态卡片 -->
    <div class="bg-slate-900 border border-slate-800 rounded-xl p-6 mb-6">
      <div class="flex items-center gap-3 mb-5">
        <span class="text-3xl">🔑</span>
        <div>
          <div class="flex items-center gap-2">
            <h3 class="font-semibold text-slate-100">当前授权状态</h3>
            <span
              class="px-2 py-0.5 rounded text-xs font-medium"
              :class="statusBadge"
            >{{ statusText }}</span>
          </div>
        </div>
      </div>

      <div class="grid grid-cols-2 gap-4 text-sm">
        <div class="bg-slate-800/60 rounded-lg p-3">
          <div class="text-slate-500 text-xs mb-1">授权版本</div>
          <div class="text-slate-100 font-medium">{{ versionText }}</div>
        </div>
        <div class="bg-slate-800/60 rounded-lg p-3">
          <div class="text-slate-500 text-xs mb-1">到期时间</div>
          <div class="text-slate-100 font-medium">{{ expireText }}</div>
        </div>
        <div class="bg-slate-800/60 rounded-lg p-3">
          <div class="text-slate-500 text-xs mb-1">剩余天数</div>
          <div class="text-slate-100 font-medium" :class="daysLeft !== null && daysLeft <= 30 ? 'text-orange-400' : ''">
            {{ daysLeft === null ? '—' : daysLeft + ' 天' }}
          </div>
        </div>
        <div class="bg-slate-800/60 rounded-lg p-3">
          <div class="text-slate-500 text-xs mb-1">授权说明</div>
          <div class="text-slate-100 font-medium">{{ reason }}</div>
        </div>
      </div>

      <!-- 机器指纹 -->
      <div class="mt-5">
        <div class="text-slate-500 text-xs mb-1.5">本机机器码（发给厂商生成激活码）</div>
        <div class="flex items-center gap-2">
          <code class="flex-1 bg-black/40 border border-slate-700 rounded-lg px-3 py-2 text-sm text-cyan-300 select-all">
            {{ fingerprint }}
          </code>
          <button
            @click="copyFingerprint"
            class="px-3 py-2 text-xs rounded-lg bg-slate-800 border border-slate-700 text-slate-300 hover:bg-slate-700 transition-colors"
          >复制</button>
        </div>
        <p class="text-[11px] text-slate-500 mt-1.5">
          激活码与机器码绑定，更换服务器后需重新申请激活码。<br />
          授权联系邮箱：<a href="mailto:x1280455974@163.com" class="text-cyan-400 hover:underline">x1280455974@163.com</a>
        </p>
      </div>
    </div>

    <!-- 激活表单 -->
    <div class="bg-slate-900 border border-slate-800 rounded-xl p-6">
      <h3 class="font-semibold text-slate-100 mb-1.5">激活 / 续期授权</h3>
      <p class="text-xs text-slate-500 mb-4">
        激活码请通过授权联系邮箱获取：<a href="mailto:x1280455974@163.com" class="text-cyan-400 hover:underline">x1280455974@163.com</a>，将上方机器码发给厂商即可生成。
      </p>
      <textarea
        v-model="licenseCode"
        rows="4"
        class="w-full bg-slate-800 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:border-cyan-500 focus:outline-none font-mono"
        placeholder="粘贴激活码…"
      ></textarea>
      <div class="flex items-center gap-3 mt-4">
        <button
          @click="activate"
          :disabled="activating || !licenseCode.trim()"
          class="px-5 py-2 text-sm rounded-lg bg-cyan-500/20 text-cyan-400 border border-cyan-500/30 hover:bg-cyan-500/30 transition-colors disabled:opacity-50"
        >{{ activating ? '激活中…' : '立即激活' }}</button>
        <span v-if="message" class="text-sm" :class="messageOk ? 'text-green-400' : 'text-red-400'">{{ message }}</span>
      </div>
      <p class="text-[11px] text-slate-500 mt-4">
        授权规则：测试版功能全开，有效期 3 个月，到期后平台锁定（仅授权页可用）；全功能版永久授权，不限制。
      </p>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { getLicenseStatus, getLicenseFingerprint, activateLicense } from '../api'

const status = ref({})
const fingerprint = ref('')
const licenseCode = ref('')
const activating = ref(false)
const message = ref('')
const messageOk = ref(false)

const statusText = computed(() => {
  const s = status.value
  if (!s.enabled) return '未启用'
  if (!s.activated) return '未激活'
  if (s.locked) return '已锁定'
  return s.permanent ? '已激活' : '试用中'
})

const statusBadge = computed(() => {
  const s = status.value
  if (!s.enabled) return 'bg-slate-800 text-slate-400'
  if (!s.activated) return 'bg-red-500/15 text-red-400'
  if (s.locked) return 'bg-red-500/15 text-red-400'
  return s.permanent ? 'bg-green-500/15 text-green-400' : 'bg-cyan-500/15 text-cyan-400'
})

const versionText = computed(() => {
  const s = status.value
  if (!s.activated) return '—'
  return s.version === 'full' ? '全功能版' : '测试版'
})

const expireText = computed(() => {
  const s = status.value
  if (!s.activated) return '—'
  if (s.permanent) return '永久'
  return s.expires_at ? s.expires_at.slice(0, 10) : '—'
})

const daysLeft = computed(() => status.value.days_left)
const reason = computed(() => status.value.reason || '—')

async function load() {
  try {
    const [st, fp] = await Promise.all([getLicenseStatus(), getLicenseFingerprint()])
    status.value = st
    fingerprint.value = fp.fingerprint || ''
  } catch (err) {
    console.error('Failed to load license:', err)
  }
}

async function copyFingerprint() {
  try {
    await navigator.clipboard.writeText(fingerprint.value)
  } catch (e) {
    const ta = document.createElement('textarea')
    ta.value = fingerprint.value
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
  }
  message.value = '机器码已复制'
  messageOk.value = true
  setTimeout(() => (message.value = ''), 2000)
}

async function activate() {
  activating.value = true
  message.value = ''
  try {
    const res = await activateLicense(licenseCode.value.trim())
    messageOk.value = res.ok
    message.value = res.message
    if (res.ok) {
      licenseCode.value = ''
      status.value = res.status || status.value
    }
  } catch (err) {
    messageOk.value = false
    message.value = err.response?.data?.detail || '激活失败，请检查网络'
  } finally {
    activating.value = false
  }
}

onMounted(load)
</script>
