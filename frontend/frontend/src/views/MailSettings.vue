<template>
  <div class="p-6 max-w-3xl mx-auto animate-in">
    <div class="flex items-center justify-between mb-6">
      <div>
        <h2 class="text-xl font-bold text-ink-strong">邮件告警设置</h2>
        <p class="text-sm text-ink-faint mt-1">SMTP 邮件通知：设备离线、告警规则触发及恢复时自动发送</p>
      </div>
      <span v-if="isAdmin" class="px-2 py-0.5 rounded text-xs bg-cyan-500/10 text-cyan-400 border border-cyan-500/30">管理员</span>
    </div>

    <!-- 非管理员提示 -->
    <div v-if="!isAdmin" class="bg-surface border border-line rounded-xl p-8 text-center">
      <div class="text-3xl mb-3">🔒</div>
      <p class="text-sm text-ink-muted">仅管理员可查看和配置邮件告警</p>
    </div>

    <!-- 邮件告警配置表单（仅管理员） -->
    <div v-else class="bg-surface border border-line rounded-xl p-6 space-y-5">
      <!-- 总开关 -->
      <div class="flex items-center justify-between">
        <div>
          <div class="text-sm font-medium text-ink">启用邮件告警</div>
          <div class="text-xs text-ink-faint mt-1">开启后自动发送设备离线 / 恢复 / 规则触发通知</div>
        </div>
        <button
          @click="mailCfg.enabled = !mailCfg.enabled"
          class="relative w-11 h-6 rounded-full transition-colors focus:outline-none shrink-0"
          :class="mailCfg.enabled ? 'bg-cyan-600' : 'bg-hover'"
        >
          <span
            class="absolute top-0.5 w-5 h-5 rounded-full bg-white transition-all"
            :class="mailCfg.enabled ? 'left-[22px]' : 'left-0.5'"
          />
        </button>
      </div>

      <div class="border-t border-line" />

      <!-- SMTP 配置 -->
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div>
          <label class="text-xs text-ink-faint font-medium block mb-1.5">SMTP 服务器</label>
          <input
            v-model="mailCfg.smtp_host"
            placeholder="smtp.example.com"
            class="w-full bg-surface-2 border border-line rounded-lg px-3 py-2 text-sm text-ink focus:border-cyan-500 focus:outline-none"
          />
        </div>
        <div>
          <label class="text-xs text-ink-faint font-medium block mb-1.5">端口</label>
          <input
            v-model.number="mailCfg.smtp_port"
            type="number"
            class="w-full bg-surface-2 border border-line rounded-lg px-3 py-2 text-sm text-ink focus:border-cyan-500 focus:outline-none"
          />
        </div>
        <div>
          <label class="text-xs text-ink-faint font-medium block mb-1.5">发信账号</label>
          <input
            v-model="mailCfg.smtp_user"
            placeholder="发信账号"
            class="w-full bg-surface-2 border border-line rounded-lg px-3 py-2 text-sm text-ink focus:border-cyan-500 focus:outline-none"
          />
        </div>
        <div>
          <label class="text-xs text-ink-faint font-medium block mb-1.5">发信密码</label>
          <input
            v-model="mailCfg.smtp_password"
            type="password"
            placeholder="留空则不修改"
            class="w-full bg-surface-2 border border-line rounded-lg px-3 py-2 text-sm text-ink focus:border-cyan-500 focus:outline-none"
          />
        </div>
        <div>
          <label class="text-xs text-ink-faint font-medium block mb-1.5">发件人地址</label>
          <input
            v-model="mailCfg.sender"
            placeholder="aiops@example.com"
            class="w-full bg-surface-2 border border-line rounded-lg px-3 py-2 text-sm text-ink focus:border-cyan-500 focus:outline-none"
          />
        </div>
        <div>
          <label class="text-xs text-ink-faint font-medium block mb-1.5">收件人地址</label>
          <input
            v-model="mailCfg.recipients"
            placeholder="多个邮箱用逗号分隔"
            class="w-full bg-surface-2 border border-line rounded-lg px-3 py-2 text-sm text-ink focus:border-cyan-500 focus:outline-none"
          />
        </div>
      </div>

      <label class="flex items-center gap-2 text-xs text-ink-muted cursor-pointer">
        <input type="checkbox" v-model="mailCfg.use_ssl" class="accent-cyan-500" /> 使用 SSL 加密连接
      </label>

      <div class="border-t border-line" />

      <div class="flex items-center gap-3">
        <button
          @click="saveMailConfig"
          :disabled="mailSaving"
          class="px-5 py-2 text-sm rounded-lg bg-cyan-500/20 text-cyan-400 border border-cyan-500/30 hover:bg-cyan-500/30 transition-colors disabled:opacity-50"
        >
          {{ mailSaving ? '保存中...' : '保存配置' }}
        </button>
        <span v-if="mailMsg" class="text-sm" :class="mailMsgOk ? 'text-green-400' : 'text-red-400'">{{ mailMsg }}</span>
      </div>

      <p class="text-xs text-ink-faint">同一事件 5 分钟内不重复发送。</p>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getMe, getMailSetting, saveMailSetting } from '../api/index.js'

const isAdmin = ref(false)
const mailCfg = ref({ enabled: false, smtp_host: '', smtp_port: 465, smtp_user: '', smtp_password: '', use_ssl: true, sender: '', recipients: '' })
const mailSaving = ref(false)
const mailMsg = ref('')
const mailMsgOk = ref(false)

async function loadMailSetting() {
  try {
    const me = await getMe()
    isAdmin.value = me.data?.role === 'admin' || me.role === 'admin'
    if (!isAdmin.value) return
    const data = await getMailSetting()
    mailCfg.value = { ...mailCfg.value, ...(data.data || data) }
  } catch (e) { /* 非管理员 403 静默 */ }
}

async function saveMailConfig() {
  mailSaving.value = true
  mailMsg.value = ''
  try {
    const data = await saveMailSetting(mailCfg.value)
    mailCfg.value = { ...mailCfg.value, ...(data.data || data) }
    mailMsgOk.value = true
    mailMsg.value = '邮件告警配置已保存'
    setTimeout(() => (mailMsg.value = ''), 3000)
  } catch (e) {
    mailMsgOk.value = false
    mailMsg.value = '保存失败：' + (e.response?.data?.detail || e.message || '未知错误')
  } finally {
    mailSaving.value = false
  }
}

onMounted(loadMailSetting)
</script>
