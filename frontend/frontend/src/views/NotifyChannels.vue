<template>
  <div class="p-6 max-w-5xl mx-auto animate-in">
    <div class="flex items-center justify-between mb-6">
      <div>
        <h2 class="text-xl font-bold text-ink-strong">通知通道</h2>
        <p class="text-sm text-ink-faint mt-1">
          邮件与即时消息统一在此配置：SMTP 邮件、钉钉 / 企业微信 / 飞书群机器人，以及自定义 Webhook
        </p>
      </div>
      <span v-if="isAdmin" class="px-2 py-0.5 rounded text-xs bg-cyan-500/10 text-cyan-400 border border-cyan-500/30">管理员</span>
    </div>

    <!-- 非管理员提示 -->
    <div v-if="!isAdmin" class="bg-surface border border-line rounded-xl p-8 text-center">
      <div class="text-3xl mb-3">🔒</div>
      <p class="text-sm text-ink-muted">仅管理员可查看和配置通知通道</p>
    </div>

    <template v-else>
      <!-- 邮件通知（SMTP）：由原「邮件告警」页面并入，与 IM 通道统一入口 -->
      <div class="mb-5 overflow-hidden rounded-xl border border-line bg-surface">
        <div class="flex items-center justify-between gap-4 px-5 py-4">
          <div class="flex min-w-0 items-center gap-3">
            <div class="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-cyan-500/20 bg-cyan-500/10 text-base">📧</div>
            <div class="min-w-0">
              <div class="flex items-center gap-2">
                <span class="font-medium text-ink-strong">邮件通知（SMTP）</span>
                <span
                  class="rounded border px-2 py-0.5 text-xs"
                  :class="mailCfg.enabled ? 'border-green-500/30 bg-green-500/10 text-green-400' : 'border-line bg-surface-2 text-ink-faint'"
                >
                  {{ mailCfg.enabled ? '已启用' : '未启用' }}
                </span>
              </div>
              <div class="mt-0.5 truncate text-xs text-ink-faint">{{ mailSummary }}</div>
            </div>
          </div>
          <button
            @click="showMailForm = !showMailForm"
            class="shrink-0 rounded-lg border border-line px-3 py-1.5 text-xs text-ink-muted transition-colors hover:bg-hover"
          >
            {{ showMailForm ? '收起' : '配置' }}
          </button>
        </div>

        <div v-if="showMailForm" class="space-y-4 border-t border-line px-5 py-4">
          <div class="flex items-center justify-between">
            <div>
              <div class="text-sm font-medium text-ink">启用邮件通知</div>
              <div class="mt-1 text-xs text-ink-faint">设备离线 / 恢复、告警规则触发时自动发送邮件</div>
            </div>
            <button
              @click="mailCfg.enabled = !mailCfg.enabled"
              class="relative h-6 w-11 shrink-0 rounded-full transition-colors focus:outline-none"
              :class="mailCfg.enabled ? 'bg-cyan-600' : 'bg-hover'"
            >
              <span
                class="absolute top-0.5 h-5 w-5 rounded-full bg-white transition-all"
                :class="mailCfg.enabled ? 'left-[22px]' : 'left-0.5'"
              />
            </button>
          </div>

          <div class="border-t border-line" />

          <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <label class="mb-1 block text-sm font-medium text-ink-muted">SMTP 服务器</label>
              <input v-model="mailCfg.smtp_host" placeholder="smtp.example.com" class="input" />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium text-ink-muted">端口</label>
              <input v-model.number="mailCfg.smtp_port" type="number" class="input" />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium text-ink-muted">发信账号</label>
              <input v-model="mailCfg.smtp_user" placeholder="发信账号" class="input" />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium text-ink-muted">发信密码</label>
              <input v-model="mailCfg.smtp_password" type="password" placeholder="留空则不修改" class="input" />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium text-ink-muted">发件人地址</label>
              <input v-model="mailCfg.sender" placeholder="aiops@example.com" class="input" />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium text-ink-muted">收件人地址</label>
              <input v-model="mailCfg.recipients" placeholder="多个邮箱用逗号分隔" class="input" />
            </div>
          </div>

          <label class="flex cursor-pointer items-center gap-2 text-xs text-ink-muted">
            <input v-model="mailCfg.use_ssl" type="checkbox" class="accent-cyan-500" /> 使用 SSL 加密连接
          </label>

          <div class="flex items-center gap-3 border-t border-line pt-4">
            <button @click="saveMailConfig" :disabled="mailSaving" class="btn btn-primary">
              {{ mailSaving ? '保存中...' : '保存配置' }}
            </button>
            <span v-if="mailMsg" class="text-sm" :class="mailMsgOk ? 'text-green-400' : 'text-red-400'">{{ mailMsg }}</span>
          </div>
        </div>
      </div>

      <!-- 说明 -->
      <div class="bg-surface border border-line rounded-xl p-4 mb-5 text-xs text-ink-faint space-y-1">
        <p>· 仅推送<b class="text-ink-muted">严重级别 ≥ 门槛</b>的告警；告警恢复通知同样适用该门槛。</p>
        <p>· 同一事件同一通道 <b class="text-ink-muted">5 分钟内只发一次</b>，防止刷屏；被收敛抑制（上游离线 / 告警风暴）的告警不推送。</p>
        <p>· Webhook 地址与加签密钥均<b class="text-ink-muted">加密存储</b>，列表只显示掩码；留空表示不修改原值。</p>
      </div>

      <!-- 即时消息通道 -->
      <div class="mb-3 flex items-center justify-between">
        <h3 class="text-sm font-semibold text-ink-strong">即时消息通道</h3>
        <div class="flex items-center gap-3">
          <span class="text-xs text-ink-faint">共 {{ channels.length }} 个</span>
          <button
            @click="openCreate"
            class="rounded-lg border border-cyan-500/30 bg-cyan-500/10 px-3 py-1.5 text-xs text-cyan-400 transition-colors hover:bg-cyan-500/20"
          >
            + 添加通道
          </button>
        </div>
      </div>

      <!-- 提示条 -->
      <div v-if="msg" class="mb-4 px-4 py-2.5 rounded-lg text-sm border"
           :class="msgOk ? 'bg-green-500/10 text-green-400 border-green-500/30' : 'bg-red-500/10 text-red-400 border-red-500/30'">
        {{ msg }}
      </div>

      <!-- 列表 -->
      <div class="bg-surface border border-line rounded-xl overflow-hidden">
        <div v-if="loading" class="p-8 text-center text-sm text-ink-faint">加载中...</div>

        <div v-else-if="channels.length === 0" class="p-12 text-center">
          <div class="text-3xl mb-3">📭</div>
          <p class="text-sm text-ink-muted mb-1">还没有配置通知通道</p>
          <p class="text-xs text-ink-faint">当前告警仅通过邮件发送；添加群机器人后可在群里实时接收</p>
        </div>

        <table v-else class="w-full text-left text-sm">
          <thead class="border-b border-line bg-surface-2 text-ink-muted text-xs uppercase tracking-wider">
            <tr>
              <th class="px-4 py-3 font-medium">通道名称</th>
              <th class="px-4 py-3 font-medium">类型</th>
              <th class="px-4 py-3 font-medium">级别门槛</th>
              <th class="px-4 py-3 font-medium">通知范围</th>
              <th class="px-4 py-3 font-medium">启用</th>
              <th class="px-4 py-3 font-medium">最近发送</th>
              <th class="px-4 py-3 font-medium text-right">操作</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-line">
            <tr v-for="ch in channels" :key="ch.id" class="hover:bg-hover/30 transition-colors">
              <td class="px-4 py-3">
                <div class="font-medium text-ink-strong">{{ ch.name }}</div>
                <div class="mt-0.5 text-xs text-ink-faint font-mono truncate max-w-[22rem]" :title="ch.webhook_url">
                  {{ ch.webhook_url || '（未设置地址）' }}
                </div>
              </td>
              <td class="px-4 py-3">
                <span class="inline-flex items-center rounded-md bg-surface-2 px-2 py-1 text-xs font-medium text-ink-muted border border-line">
                  {{ typeLabel(ch.channel_type) }}
                </span>
              </td>
              <td class="px-4 py-3">
                <span class="inline-flex items-center rounded-md px-2 py-1 text-xs font-medium border"
                      :class="severityClass(ch.min_severity)">
                  ≥ {{ severityLabel(ch.min_severity) }}
                </span>
              </td>
              <td class="px-4 py-3 text-xs text-ink-muted">
                <span v-if="ch.mention_all" class="text-cyan-400">@所有人</span>
                <span v-else>不 @人</span>
                <span class="ml-2 text-ink-faint">重试 {{ ch.retry_times }} 次</span>
              </td>
              <td class="px-4 py-3">
                <button
                  @click="toggleEnabled(ch)"
                  class="relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none"
                  :class="ch.enabled ? 'bg-cyan-600' : 'bg-hover'"
                >
                  <span class="inline-block h-4 w-4 transform rounded-full bg-white transition-transform"
                        :class="ch.enabled ? 'translate-x-6' : 'translate-x-1'" />
                </button>
              </td>
              <td class="px-4 py-3 text-xs max-w-[14rem]">
                <div v-if="ch.last_sent_at" class="text-ink-muted">{{ formatDateTime(ch.last_sent_at) }}</div>
                <div v-if="ch.last_result" class="mt-0.5 truncate"
                     :class="ch.last_result.includes('成功') ? 'text-green-400' : 'text-red-400'"
                     :title="ch.last_result">
                  {{ ch.last_result }}
                </div>
                <div v-else class="text-ink-faint">尚未发送</div>
              </td>
              <td class="px-4 py-3">
                <div class="flex items-center justify-end gap-1.5">
                  <button
                    @click="doTest(ch)"
                    :disabled="testingId === ch.id"
                    class="px-2.5 py-1 text-xs rounded bg-cyan-500/10 text-cyan-400 border border-cyan-500/20 hover:bg-cyan-500/20 transition-colors disabled:opacity-50"
                  >
                    {{ testingId === ch.id ? '发送中...' : '测试' }}
                  </button>
                  <button
                    @click="openEdit(ch)"
                    class="px-2.5 py-1 text-xs rounded text-blue-400 hover:bg-blue-600/20 transition-colors"
                  >
                    编辑
                  </button>
                  <button
                    @click="askDelete(ch)"
                    class="px-2.5 py-1 text-xs rounded text-red-400 hover:bg-red-600/20 transition-colors"
                  >
                    删除
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- 弹窗统一 Teleport 到 body：脱离页面容器，避免被祖先的 transform / overflow 影响定位或被裁切 -->
    <Teleport to="body">
    <!-- 新增 / 编辑弹窗 -->
    <div v-if="showModal" class="fixed inset-0 z-50 flex overflow-y-auto bg-black/60 p-4" @click.self="closeModal">
      <div class="m-auto w-full max-w-lg rounded-xl border border-line bg-surface shadow-2xl">
        <div class="border-b border-line px-6 py-4">
          <h2 class="text-lg font-semibold text-ink-strong">{{ isEditing ? '编辑通知通道' : '添加通知通道' }}</h2>
        </div>

        <div class="max-h-[65vh] space-y-4 overflow-y-auto px-6 py-4">
          <div>
            <label class="mb-1 block text-sm font-medium text-ink-muted">通道类型</label>
            <div class="grid grid-cols-2 gap-2">
              <button
                v-for="t in channelTypes" :key="t.value" type="button"
                @click="form.channel_type = t.value"
                class="rounded-lg border px-3 py-2 text-sm text-left transition-colors"
                :class="form.channel_type === t.value
                  ? 'border-cyan-500 bg-cyan-500/10 text-cyan-300'
                  : 'border-line bg-surface-2 text-ink-muted hover:bg-hover'"
              >
                {{ t.label }}
              </button>
            </div>
          </div>

          <div>
            <label class="mb-1 block text-sm font-medium text-ink-muted">通道名称</label>
            <input v-model="form.name" type="text" class="input" placeholder="如：运维告警群（钉钉）" />
          </div>

          <div>
            <label class="mb-1 block text-sm font-medium text-ink-muted">Webhook 地址</label>
            <input v-model="form.webhook_url" type="text" class="input font-mono text-xs"
                   :placeholder="urlPlaceholder" />
            <p class="mt-1 text-xs text-ink-faint">
              留空或保留掩码表示不修改原地址。{{ typeHint }}
            </p>
          </div>

          <div v-if="form.channel_type === 'dingtalk'">
            <label class="mb-1 block text-sm font-medium text-ink-muted">加签密钥（可选）</label>
            <input v-model="form.secret" type="password" class="input"
                   :placeholder="isEditing ? '留空则不修改' : '钉钉安全设置选「加签」时填写 SEC 开头字符串'" />
            <p class="mt-1 text-xs text-ink-faint">机器人安全设置选择「加签」时必填，否则钉钉会拒收消息</p>
          </div>

          <div class="grid grid-cols-2 gap-4">
            <div>
              <label class="mb-1 block text-sm font-medium text-ink-muted">级别门槛</label>
              <select v-model="form.min_severity"
                      class="w-full rounded-lg border border-line bg-surface-2 px-3 py-2 text-sm text-ink-strong focus:border-cyan-500 focus:outline-none focus:ring-1 focus:ring-cyan-500">
                <option v-for="s in severities" :key="s.value" :value="s.value">≥ {{ s.label }}</option>
              </select>
              <p class="mt-1 text-xs text-ink-faint">低于该级别的告警不推送</p>
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium text-ink-muted">发送失败重试次数</label>
              <input v-model.number="form.retry_times" type="number" min="0" max="3" class="input" />
              <p class="mt-1 text-xs text-ink-faint">0 ~ 3 次</p>
            </div>
          </div>

          <div class="flex items-center gap-6">
            <label class="flex items-center gap-2 text-sm text-ink-muted cursor-pointer">
              <input type="checkbox" v-model="form.mention_all" class="accent-cyan-500" /> @所有人
            </label>
            <label class="flex items-center gap-2 text-sm text-ink-muted cursor-pointer">
              <input type="checkbox" v-model="form.enabled" class="accent-cyan-500" /> 启用此通道
            </label>
          </div>
          <p v-if="form.channel_type === 'feishu' || form.channel_type === 'webhook'" class="text-xs text-ink-faint">
            {{ form.channel_type === 'webhook' ? '自定义 Webhook 以固定 JSON 结构 POST（含 source/title/text/severity），便于对接自建系统。' : '飞书机器人以纯文本消息发送，暂不支持 @所有人。' }}
          </p>
        </div>

        <div class="flex justify-end gap-3 border-t border-line px-6 py-4">
          <button @click="closeModal" class="rounded-lg border border-line px-4 py-2 text-sm text-ink-muted hover:bg-hover transition-colors">
            取消
          </button>
          <button @click="submit" :disabled="saving" class="btn btn-primary">
            {{ saving ? '保存中...' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 删除确认 -->
    <div v-if="deleteTarget" class="fixed inset-0 z-50 flex overflow-y-auto bg-black/60 p-4" @click.self="deleteTarget = null">
      <div class="m-auto w-full max-w-sm rounded-xl border border-line bg-surface shadow-2xl">
        <div class="px-6 py-6">
          <h3 class="text-base font-semibold text-ink-strong">确认删除</h3>
          <p class="mt-2 text-sm text-ink-muted">
            确定删除通道「{{ deleteTarget.name }}」吗？删除后该群将不再收到告警通知。
          </p>
        </div>
        <div class="flex justify-end gap-3 border-t border-line px-6 py-4">
          <button @click="deleteTarget = null" class="rounded-lg border border-line px-4 py-2 text-sm text-ink-muted hover:bg-hover transition-colors">
            取消
          </button>
          <button @click="confirmDelete" :disabled="deleting" class="btn btn-danger">
            {{ deleting ? '删除中...' : '删除' }}
          </button>
        </div>
      </div>
    </div>
    </Teleport>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import {
  getMe,
  getMailSetting,
  saveMailSetting,
  getNotifyChannelMeta,
  getNotifyChannels,
  createNotifyChannel,
  updateNotifyChannel,
  deleteNotifyChannel,
  testNotifyChannel,
} from '../api/index.js'

const isAdmin = ref(false)
const loading = ref(true)
const channels = ref([])
const channelTypes = ref([])
const severities = ref([])
const msg = ref('')
const msgOk = ref(true)
const testingId = ref(null)

const showModal = ref(false)
const isEditing = ref(false)
const editingId = ref(null)
const saving = ref(false)
const deleteTarget = ref(null)
const deleting = ref(false)

// 邮件通知（SMTP）—— 由原「邮件告警」页面并入本页
const showMailForm = ref(false)
const mailCfg = ref({
  enabled: false,
  smtp_host: '',
  smtp_port: 465,
  smtp_user: '',
  smtp_password: '',
  use_ssl: true,
  sender: '',
  recipients: '',
})
const mailSaving = ref(false)
const mailMsg = ref('')
const mailMsgOk = ref(true)

const mailSummary = computed(() => {
  const cfg = mailCfg.value
  if (!cfg.smtp_host) return '尚未配置 SMTP 服务器，点「配置」填写'
  const rcp = (cfg.recipients || '').trim()
  return `${cfg.smtp_host}:${cfg.smtp_port || '-'}${rcp ? ' → ' + rcp : '（未填收件人）'}`
})

const defaultForm = () => ({
  name: '',
  channel_type: 'dingtalk',
  webhook_url: '',
  secret: '',
  enabled: true,
  min_severity: 'warning',
  mention_all: false,
  retry_times: 1,
})
const form = reactive(defaultForm())

const DEFAULT_TYPES = [
  { value: 'dingtalk', label: '钉钉群机器人' },
  { value: 'wecom', label: '企业微信群机器人' },
  { value: 'feishu', label: '飞书群机器人' },
  { value: 'webhook', label: '自定义 Webhook' },
]
const DEFAULT_SEV = [
  { value: 'info', label: '提示' },
  { value: 'warning', label: '警告' },
  { value: 'minor', label: '次要' },
  { value: 'major', label: '重要' },
  { value: 'critical', label: '严重' },
]

const typeLabel = (v) => (channelTypes.value.find((t) => t.value === v)?.label) || v
const severityLabel = (v) => (severities.value.find((s) => s.value === v)?.label) || v

const severityClass = (v) => ({
  critical: 'bg-red-600/20 text-red-400 border-red-600/30',
  major: 'bg-orange-600/20 text-orange-400 border-orange-600/30',
  minor: 'bg-yellow-600/20 text-yellow-400 border-yellow-600/30',
  warning: 'bg-blue-600/20 text-blue-400 border-blue-600/30',
  info: 'bg-surface-2 text-ink-muted border-line',
}[v] || 'bg-surface-2 text-ink-muted border-line')

const urlPlaceholder = computed(() => ({
  dingtalk: 'https://oapi.dingtalk.com/robot/send?access_token=...',
  wecom: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...',
  feishu: 'https://open.feishu.cn/open-apis/bot/v2/hook/...',
  webhook: 'https://your-system.example.com/aiops/alert',
}[form.channel_type] || 'https://...'))

const typeHint = computed(() => ({
  dingtalk: '群设置 → 智能群助手 → 添加机器人 → 自定义，复制 Webhook 地址。',
  wecom: '群设置 → 群机器人 → 添加 → 复制 Webhook 地址。',
  feishu: '群设置 → 群机器人 → 添加 → 自定义机器人，复制 Webhook 地址。',
  webhook: '自定义接收端，需能接收 JSON POST 请求。',
}[form.channel_type] || ''))

function formatDateTime(v) {
  if (!v) return '-'
  try { return new Date(v).toLocaleString('zh-CN') } catch { return v }
}

function flash(text, ok = true) {
  msg.value = text
  msgOk.value = ok
  setTimeout(() => { if (msg.value === text) msg.value = '' }, 4000)
}

async function loadMeta() {
  try {
    const data = await getNotifyChannelMeta()
    channelTypes.value = data?.types?.length ? data.types : DEFAULT_TYPES
    severities.value = data?.severities?.length ? data.severities : DEFAULT_SEV
  } catch {
    channelTypes.value = DEFAULT_TYPES
    severities.value = DEFAULT_SEV
  }
}

async function fetchChannels() {
  loading.value = true
  try {
    const data = await getNotifyChannels()
    channels.value = Array.isArray(data) ? data : []
  } catch (e) {
    channels.value = []
    flash('加载通道失败：' + (e.response?.data?.detail || e.message), false)
  } finally {
    loading.value = false
  }
}

async function loadMailSetting() {
  try {
    const data = await getMailSetting()
    mailCfg.value = { ...mailCfg.value, ...(data?.data || data || {}) }
  } catch { /* 非管理员由全局拦截器处理 */ }
}

async function saveMailConfig() {
  mailSaving.value = true
  mailMsg.value = ''
  try {
    const data = await saveMailSetting(mailCfg.value)
    mailCfg.value = { ...mailCfg.value, ...(data?.data || data || {}) }
    mailMsgOk.value = true
    mailMsg.value = '邮件配置已保存'
    setTimeout(() => { if (mailMsg.value === '邮件配置已保存') mailMsg.value = '' }, 3000)
  } catch (e) {
    mailMsgOk.value = false
    mailMsg.value = '保存失败：' + (e.response?.data?.detail || e.message || '未知错误')
  } finally {
    mailSaving.value = false
  }
}

function openCreate() {
  isEditing.value = false
  editingId.value = null
  Object.assign(form, defaultForm())
  showModal.value = true
}

function openEdit(ch) {
  isEditing.value = true
  editingId.value = ch.id
  Object.assign(form, {
    name: ch.name,
    channel_type: ch.channel_type,
    // 掩码回显：后端识别含 **** 的值视为未修改
    webhook_url: ch.webhook_url || '',
    secret: ch.secret || '',
    enabled: ch.enabled,
    min_severity: ch.min_severity || 'warning',
    mention_all: !!ch.mention_all,
    retry_times: ch.retry_times ?? 1,
  })
  showModal.value = true
}

function closeModal() { showModal.value = false }

async function submit() {
  if (!form.channel_type) { flash('请选择通道类型', false); return }
  if (!isEditing.value && !form.webhook_url) { flash('请填写 Webhook 地址', false); return }
  saving.value = true
  try {
    const payload = { ...form, retry_times: Number(form.retry_times) || 0 }
    if (isEditing.value) {
      await updateNotifyChannel(editingId.value, payload)
      flash('通道已更新')
    } else {
      await createNotifyChannel(payload)
      flash('通道已添加')
    }
    showModal.value = false
    await fetchChannels()
  } catch (e) {
    flash('保存失败：' + (e.response?.data?.detail || e.message || '未知错误'), false)
  } finally {
    saving.value = false
  }
}

async function toggleEnabled(ch) {
  const next = !ch.enabled
  try {
    // 局部更新：只提交 enabled，后端不会重置其他字段
    await updateNotifyChannel(ch.id, { enabled: next })
    ch.enabled = next
    flash(`已${next ? '启用' : '停用'}「${ch.name}」`)
  } catch (e) {
    flash('操作失败：' + (e.response?.data?.detail || e.message), false)
  }
}

async function doTest(ch) {
  testingId.value = ch.id
  try {
    const res = await testNotifyChannel(ch.id)
    const sent = res?.sent ?? res?.data?.sent
    if (sent) flash(`测试消息已发送到「${ch.name}」，请到群里查看`)
    else flash(`测试发送失败：${res?.reason || res?.data?.reason || '未知原因'}`, false)
    await fetchChannels()
  } catch (e) {
    flash('测试发送失败：' + (e.response?.data?.detail || e.message), false)
  } finally {
    testingId.value = null
  }
}

function askDelete(ch) { deleteTarget.value = ch }

async function confirmDelete() {
  if (!deleteTarget.value) return
  deleting.value = true
  try {
    await deleteNotifyChannel(deleteTarget.value.id)
    flash('通道已删除')
    deleteTarget.value = null
    await fetchChannels()
  } catch (e) {
    flash('删除失败：' + (e.response?.data?.detail || e.message), false)
  } finally {
    deleting.value = false
  }
}

onMounted(async () => {
  try {
    const me = await getMe()
    isAdmin.value = (me?.role || me?.data?.role) === 'admin'
  } catch { /* 未登录由全局拦截器处理 */ }
  if (!isAdmin.value) { loading.value = false; return }
  await loadMeta()
  await loadMailSetting()
  await fetchChannels()
})
</script>
