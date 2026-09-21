<template>
  <div class="min-h-screen bg-app text-ink-strong animate-in">
    <!-- Header -->
    <div class="border-b border-line bg-surface/50 px-6 py-4">
      <div class="flex items-center justify-between">
        <div>
          <h1 class="text-2xl font-bold text-ink-strong">账号管理</h1>
          <p class="mt-1 text-sm text-ink-muted">修改密码、管理平台账号与权限</p>
        </div>
        <button
          @click="handleLogout"
          :disabled="loggingOut"
          class="flex items-center gap-2 rounded-lg border border-red-800/60 bg-red-900/30 px-4 py-2 text-sm
                 text-red-300 transition-colors hover:bg-red-800/50 disabled:opacity-60"
        >
          <svg class="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                  d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
          </svg>
          {{ loggingOut ? '退出中...' : '退出登录' }}
        </button>
      </div>
    </div>

    <div class="px-6 py-6">
      <!-- 当前账号 -->
      <div class="mb-6 rounded-xl border border-line bg-surface/50 p-5">
        <h2 class="mb-4 text-sm font-semibold text-ink-muted">当前账号</h2>
        <div class="flex items-center gap-4">
          <div
            class="flex h-12 w-12 items-center justify-center rounded-full bg-gradient-to-br from-cyan-600 to-cyan-900 text-lg font-bold text-cyan-100"
          >
            {{ me.username ? me.username.charAt(0).toUpperCase() : '?' }}
          </div>
          <div>
            <div class="text-base font-semibold text-ink-strong">{{ me.username }}</div>
            <span
              :class="me.role === 'admin' ? 'bg-red-600/15 text-red-400 border-red-600/30' : me.role === 'operator' ? 'bg-amber-600/15 text-amber-400 border-amber-600/30' : 'bg-cyan-600/15 text-cyan-400 border-cyan-600/30'"
              class="mt-1 inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium"
            >
              {{ me.role === 'admin' ? '管理员 admin' : me.role === 'operator' ? '运维 operator' : '只读 viewer' }}
            </span>
          </div>
        </div>
      </div>

      <div class="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <!-- 修改我的密码 -->
        <div class="rounded-xl border border-line bg-surface/50 p-5">
          <h2 class="mb-4 text-sm font-semibold text-ink-muted">修改我的密码</h2>
          <form @submit.prevent="submitChangePassword" class="space-y-4">
            <div>
              <label class="mb-1 block text-sm font-medium text-ink-muted">旧密码</label>
              <input
                v-model="pwdForm.old_password"
                type="password"
                autocomplete="current-password"
                class="input"
                placeholder="请输入旧密码"
                required
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium text-ink-muted">新密码</label>
              <input
                v-model="pwdForm.new_password"
                type="password"
                autocomplete="new-password"
                class="input"
                placeholder="至少 12 位，含大小写字母和数字"
                required
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium text-ink-muted">确认新密码</label>
              <input
                v-model="pwdForm.confirm"
                type="password"
                autocomplete="new-password"
                class="input"
                placeholder="再次输入新密码"
                required
              />
            </div>
            <button
              type="submit"
              :disabled="pwdSubmitting"
              class="btn btn-primary"
            >
              {{ pwdSubmitting ? '提交中...' : '修改密码' }}
            </button>
            <p v-if="pwdMsg" :class="pwdMsgOk ? 'text-green-400' : 'text-red-400'" class="text-sm">
              {{ pwdMsg }}
            </p>
          </form>
        </div>

        <!-- 新建账号（仅 admin） -->
        <div v-if="isAdmin" class="rounded-xl border border-line bg-surface/50 p-5">
          <h2 class="mb-4 text-sm font-semibold text-ink-muted">新建账号</h2>
          <form @submit.prevent="submitCreateUser" class="space-y-4">
            <div>
              <label class="mb-1 block text-sm font-medium text-ink-muted">用户名</label>
              <input
                v-model="createForm.username"
                type="text"
                maxlength="64"
                class="input"
                placeholder="3-64 个字符"
                required
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium text-ink-muted">初始密码</label>
              <input
                v-model="createForm.password"
                type="password"
                class="input"
                placeholder="至少 12 位，含大小写字母和数字"
                required
              />
            </div>
            <div>
              <label class="mb-1 block text-sm font-medium text-ink-muted">角色</label>
              <select
                v-model="createForm.role"
                class="select"
              >
                <option value="viewer">viewer（只读）</option>
                <option value="operator">operator（运维）</option>
                <option value="admin">admin（管理员）</option>
              </select>
            </div>
            <button
              type="submit"
              :disabled="createSubmitting"
              class="btn btn-primary"
            >
              {{ createSubmitting ? '创建中...' : '创建账号' }}
            </button>
            <p v-if="createMsg" :class="createMsgOk ? 'text-green-400' : 'text-red-400'" class="text-sm">
              {{ createMsg }}
            </p>
          </form>
        </div>
      </div>

      <!-- 账号列表（仅 admin） -->
      <div v-if="isAdmin" class="mt-6 overflow-hidden rounded-xl border border-line bg-surface/50">
        <div class="border-b border-line px-5 py-4">
          <h2 class="text-sm font-semibold text-ink-muted">账号列表</h2>
        </div>
        <table class="w-full text-left text-sm">
          <thead class="border-b border-line bg-surface text-ink-muted">
            <tr>
              <th class="px-4 py-3 font-medium">ID</th>
              <th class="px-4 py-3 font-medium">用户名</th>
              <th class="px-4 py-3 font-medium">角色</th>
              <th class="px-4 py-3 font-medium">状态</th>
              <th class="px-4 py-3 font-medium">创建时间</th>
              <th class="px-4 py-3 font-medium text-right">操作</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-line">
            <tr v-for="user in users" :key="user.id" class="transition-colors hover:bg-hover/30">
              <td class="px-4 py-3 text-ink-muted">{{ user.id }}</td>
              <td class="px-4 py-3 font-medium text-ink-strong">{{ user.username }}</td>
              <td class="px-4 py-3">
                <span
                  :class="user.role === 'admin' ? 'bg-red-600/15 text-red-400 border-red-600/30' : user.role === 'operator' ? 'bg-amber-600/15 text-amber-400 border-amber-600/30' : 'bg-cyan-600/15 text-cyan-400 border-cyan-600/30'"
                  class="inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium"
                >
                  {{ user.role }}
                </span>
              </td>
              <td class="px-4 py-3">
                <span
                  :class="user.is_active ? 'bg-green-600/15 text-green-400 border-green-600/30' : 'bg-line-strong/15 text-ink-muted border-line-strong/30'"
                  class="inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium"
                >
                  {{ user.is_active ? '启用' : '停用' }}
                </span>
              </td>
              <td class="px-4 py-3 text-ink-muted">{{ formatTime(user.created_at) }}</td>
              <td class="px-4 py-3">
                <div class="flex items-center justify-end gap-2">
                  <button
                    @click="openResetModal(user)"
                    class="rounded-md px-2 py-1 text-xs text-cyan-400 transition-colors hover:bg-cyan-600/20 hover:text-cyan-300"
                  >
                    重置密码
                  </button>
                  <button
                    @click="toggleActive(user)"
                    :class="user.is_active ? 'text-ink-muted hover:bg-hover/30 hover:text-ink' : 'text-green-400 hover:bg-green-600/20 hover:text-green-300'"
                    class="rounded-md px-2 py-1 text-xs transition-colors"
                  >
                    {{ user.is_active ? '停用' : '启用' }}
                  </button>
                  <button
                    v-if="canDelete(user)"
                    @click="openDeleteModal(user)"
                    class="rounded-md px-2 py-1 text-xs text-red-400 transition-colors hover:bg-red-600/20 hover:text-red-300"
                  >
                    删除
                  </button>
                  <span
                    v-else
                    class="rounded-md px-2 py-1 text-xs text-ink-faint/60 cursor-not-allowed"
                    :title="deleteBlockedReason(user)"
                  >
                    删除
                  </span>
                </div>
              </td>
            </tr>
            <tr v-if="users.length === 0">
              <td colspan="6" class="px-4 py-12 text-center text-ink-faint">暂无账号</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 访问 IP 白名单（仅 admin） -->
      <div v-if="isAdmin" class="mt-6 rounded-xl border border-line bg-surface/50 p-5">
        <div class="flex items-start justify-between gap-6">
          <div class="min-w-0">
            <h2 class="text-sm font-semibold text-ink-muted">访问 IP 白名单</h2>
            <p class="mt-1 text-xs leading-relaxed text-ink-faint">
              启用后，只有名单内的 IP / 网段能打开本平台（前端页面与接口一并拦截，其他来源会看到「访问受限」提示页）。
              默认关闭 = 不限制任何 IP。改动需点下方「保存白名单配置」才生效。
            </p>
          </div>
          <div class="flex shrink-0 items-center gap-3">
            <span :class="wl.enabled ? 'text-green-400' : 'text-ink-faint'" class="text-xs font-medium">
              {{ wl.enabled ? '已启用' : '未启用' }}
            </span>
            <button
              type="button"
              role="switch"
              :aria-checked="wl.enabled ? 'true' : 'false'"
              :title="wl.enabled ? '点击关闭（关闭后不限制访问 IP）' : '点击启用（只有名单内 IP 可访问）'"
              @click="toggleWhitelist"
              class="relative inline-flex h-6 w-11 shrink-0 items-center rounded-full border transition-colors"
              :class="wl.enabled ? 'border-green-500 bg-green-600/80' : 'border-line-strong bg-line-strong/40'"
            >
              <span
                class="inline-block h-4 w-4 rounded-full bg-white shadow transition-transform"
                :class="wl.enabled ? 'translate-x-6' : 'translate-x-1'"
              />
            </button>
          </div>
        </div>

        <!-- 平台看到的当前来源 IP -->
        <div class="mt-4 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-line bg-app/40 px-3 py-2">
          <span class="text-xs text-ink-muted">平台看到的您的当前来源 IP</span>
          <code class="rounded bg-surface px-2 py-0.5 font-mono text-xs text-ink-strong">{{ wl.currentIp || '未知' }}</code>
          <span v-if="isLoopbackCurrent" class="text-xs text-amber-400">本机访问，回环地址始终放行</span>
          <template v-else>
            <span v-if="currentIpInList" class="text-xs text-green-400">已在名单内</span>
            <button
              v-else-if="wl.currentIp"
              type="button"
              @click="addCurrentIpToWhitelist"
              class="rounded-md border border-cyan-600/40 px-2 py-0.5 text-xs text-cyan-400 transition-colors hover:bg-cyan-600/20"
            >
              + 加入白名单
            </button>
          </template>
        </div>

        <!-- 添加条目 -->
        <div class="mt-4 flex flex-wrap items-end gap-3">
          <div class="min-w-[180px] flex-1">
            <label class="mb-1 block text-xs text-ink-muted">IP 或网段</label>
            <input
              v-model="wlNew.cidr"
              type="text"
              class="input font-mono"
              placeholder="192.168.1.10 或 192.168.1.0/24"
              @keyup.enter="addWhitelistEntry"
            />
          </div>
          <div class="min-w-[160px] flex-1">
            <label class="mb-1 block text-xs text-ink-muted">备注（可选）</label>
            <input
              v-model="wlNew.remark"
              type="text"
              maxlength="64"
              class="input"
              placeholder="如：办公网 / 运维笔记本"
              @keyup.enter="addWhitelistEntry"
            />
          </div>
          <button type="button" @click="addWhitelistEntry" class="btn btn-outline">添加</button>
        </div>

        <!-- 条目列表 -->
        <div class="mt-3 overflow-hidden rounded-lg border border-line">
          <table class="w-full text-left text-sm">
            <thead class="border-b border-line bg-surface text-xs text-ink-muted">
              <tr>
                <th class="px-3 py-2 font-medium">IP / 网段</th>
                <th class="px-3 py-2 font-medium">备注</th>
                <th class="px-3 py-2 font-medium">创建人</th>
                <th class="px-3 py-2 text-right font-medium">操作</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-line">
              <tr v-for="(entry, idx) in wl.entries" :key="`${entry.cidr}-${idx}`" class="transition-colors hover:bg-hover/30">
                <td class="px-3 py-2 font-mono text-ink-strong">{{ entry.cidr }}</td>
                <td class="px-3 py-2 text-ink-muted">{{ entry.remark || '-' }}</td>
                <td class="px-3 py-2 text-ink-faint">{{ entry.created_by || '未保存' }}</td>
                <td class="px-3 py-2 text-right">
                  <button
                    type="button"
                    @click="removeWhitelistEntry(idx)"
                    class="rounded-md px-2 py-1 text-xs text-red-400 transition-colors hover:bg-red-600/20 hover:text-red-300"
                  >
                    移除
                  </button>
                </td>
              </tr>
              <tr v-if="wl.entries.length === 0">
                <td colspan="4" class="px-3 py-6 text-center text-xs text-ink-faint">
                  名单为空 —— 未启用时不影响访问；启用前请至少添加一条
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 保存 -->
        <div class="mt-4 flex flex-wrap items-center gap-3">
          <button type="button" @click="saveWhitelist" :disabled="wlSaving" class="btn btn-primary">
            {{ wlSaving ? '保存中...' : '保存白名单配置' }}
          </button>
          <span v-if="wlDirty" class="text-xs text-amber-400">有未保存的修改</span>
          <p v-if="wlMsg" :class="wlMsgOk ? 'text-green-400' : 'text-red-400'" class="text-sm">{{ wlMsg }}</p>
        </div>

        <p class="mt-3 rounded-lg border border-amber-600/30 bg-amber-600/10 px-3 py-2 text-xs leading-relaxed text-amber-400/90">
          安全说明：启用后若漏掉自己的 IP 会立即无法访问平台。保存时后端会校验「提交的名单必须包含你当前的来源 IP」，
          不满足直接拒绝保存；万一仍被锁住，可在部署平台的服务器上直接用本机地址打开平台改回
          —— 本机回环地址（127.0.0.1）始终放行，名单配错锁不死自己。
        </p>
      </div>
    </div>

    <!-- 重置密码 Modal -->
    <div
      v-if="showResetModal"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      @click.self="closeResetModal"
    >
      <div class="w-full max-w-sm rounded-xl border border-line bg-surface shadow-2xl">
        <div class="border-b border-line px-6 py-4">
          <h2 class="text-lg font-semibold text-ink-strong">重置密码</h2>
        </div>
        <div class="px-6 py-4">
          <p class="mb-3 text-sm text-ink-muted">
            为账号 <span class="font-medium text-ink">{{ resetTarget?.username }}</span> 设置新密码
          </p>
          <input
            v-model="resetPwd"
            type="password"
            class="input"
            placeholder="至少 12 位，含大小写字母和数字"
          />
          <p v-if="resetMsg" :class="resetMsgOk ? 'text-green-400' : 'text-red-400'" class="mt-2 text-sm">
            {{ resetMsg }}
          </p>
        </div>
        <div class="flex justify-end gap-3 border-t border-line px-6 py-4">
          <button
            @click="closeResetModal"
            class="btn btn-outline"
          >
            取消
          </button>
          <button
            @click="confirmReset"
            :disabled="resetSubmitting"
            class="btn btn-primary"
          >
            {{ resetSubmitting ? '重置中...' : '确认重置' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 删除账号 Modal -->
    <div
      v-if="showDeleteModal"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      @click.self="closeDeleteModal"
    >
      <div class="w-full max-w-sm rounded-xl border border-line bg-surface shadow-2xl">
        <div class="border-b border-line px-6 py-4">
          <h2 class="text-lg font-semibold text-red-400">删除账号</h2>
        </div>
        <div class="px-6 py-4">
          <p class="mb-2 text-sm text-ink-muted">
            确定要删除账号
            <span class="font-medium text-ink">{{ deleteTarget?.username }}</span>
            （角色 {{ deleteTarget?.role }}）吗？
          </p>
          <p class="text-sm text-red-400/90">删除后该账号立即无法登录，且不可恢复。</p>
          <p v-if="deleteMsg" class="mt-2 text-sm text-red-400">{{ deleteMsg }}</p>
        </div>
        <div class="flex justify-end gap-3 border-t border-line px-6 py-4">
          <button
            @click="closeDeleteModal"
            class="btn btn-outline"
          >
            取消
          </button>
          <button
            @click="confirmDelete"
            :disabled="deleteSubmitting"
            class="btn btn-danger"
          >
            {{ deleteSubmitting ? '删除中...' : '确认删除' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { getMe, getUsers, createUser, updateUser, deleteUser, changePassword, logout, getIpWhitelist, saveIpWhitelist } from '../api/index.js'

const router = useRouter()
const me = ref({ username: '', role: '' })
const users = ref([])
const isAdmin = computed(() => me.value.role === 'admin')

// 退出登录
const loggingOut = ref(false)
async function handleLogout() {
  if (!confirm('确定要退出登录吗？')) return
  loggingOut.value = true
  try {
    await logout()
  } catch { /* 忽略网络错误，cookie 由前端清理 */ }
  router.push('/login')
}

// 修改密码
const pwdForm = ref({ old_password: '', new_password: '', confirm: '' })
const pwdSubmitting = ref(false)
const pwdMsg = ref('')
const pwdMsgOk = ref(false)

// 新建账号
const createForm = ref({ username: '', password: '', role: 'viewer' })
const createSubmitting = ref(false)
const createMsg = ref('')
const createMsgOk = ref(false)

// 重置密码
const showResetModal = ref(false)
const resetTarget = ref(null)
const resetPwd = ref('')
const resetSubmitting = ref(false)
const resetMsg = ref('')
const resetMsgOk = ref(false)

// 删除账号
const showDeleteModal = ref(false)
const deleteTarget = ref(null)
const deleteSubmitting = ref(false)
const deleteMsg = ref('')

const PWD_HINT = '至少 12 位，且同时包含大写字母、小写字母和数字'

function checkPwdStrength(pwd) {
  if (pwd.length < 12) return '密码至少需要 12 位'
  if (!/[a-z]/.test(pwd)) return '密码需包含小写字母'
  if (!/[A-Z]/.test(pwd)) return '密码需包含大写字母'
  if (!/\d/.test(pwd)) return '密码需包含数字'
  return null
}

function formatTime(value) {
  if (!value) return '-'
  return String(value).slice(0, 19).replace('T', ' ')
}

async function loadMe() {
  try {
    me.value = await getMe()
  } catch (e) {
    console.error('loadMe failed:', e)
  }
}

async function loadUsers() {
  if (!isAdmin.value) return
  try {
    users.value = await getUsers()
  } catch (e) {
    console.error('loadUsers failed:', e)
  }
}

async function submitChangePassword() {
  pwdMsg.value = ''
  if (pwdForm.value.new_password !== pwdForm.value.confirm) {
    pwdMsg.value = '两次输入的新密码不一致'
    pwdMsgOk.value = false
    return
  }
  const err = checkPwdStrength(pwdForm.value.new_password)
  if (err) {
    pwdMsg.value = err
    pwdMsgOk.value = false
    return
  }
  pwdSubmitting.value = true
  try {
    await changePassword({
      old_password: pwdForm.value.old_password,
      new_password: pwdForm.value.new_password,
    })
    pwdMsg.value = '密码修改成功！下次登录请使用新密码。'
    pwdMsgOk.value = true
    pwdForm.value = { old_password: '', new_password: '', confirm: '' }
  } catch (e) {
    pwdMsg.value = e.response?.data?.detail || '修改失败，请检查旧密码'
    pwdMsgOk.value = false
  }
  pwdSubmitting.value = false
}

async function submitCreateUser() {
  createMsg.value = ''
  const err = checkPwdStrength(createForm.value.password)
  if (err) {
    createMsg.value = err
    createMsgOk.value = false
    return
  }
  createSubmitting.value = true
  try {
    await createUser({
      username: createForm.value.username,
      password: createForm.value.password,
      role: createForm.value.role,
    })
    createMsg.value = `账号「${createForm.value.username}」创建成功！`
    createMsgOk.value = true
    createForm.value = { username: '', password: '', role: 'viewer' }
    loadUsers()
  } catch (e) {
    createMsg.value = e.response?.data?.detail || '创建失败'
    createMsgOk.value = false
  }
  createSubmitting.value = false
}

function openResetModal(user) {
  resetTarget.value = user
  resetPwd.value = ''
  resetMsg.value = ''
  showResetModal.value = true
}

function closeResetModal() {
  showResetModal.value = false
  resetTarget.value = null
}

async function confirmReset() {
  resetMsg.value = ''
  const err = checkPwdStrength(resetPwd.value)
  if (err) {
    resetMsg.value = err
    resetMsgOk.value = false
    return
  }
  resetSubmitting.value = true
  try {
    await updateUser(resetTarget.value.id, { password: resetPwd.value })
    resetMsg.value = `已重置「${resetTarget.value.username}」的密码`
    resetMsgOk.value = true
    closeResetModal()
  } catch (e) {
    resetMsg.value = e.response?.data?.detail || '重置失败'
    resetMsgOk.value = false
  }
  resetSubmitting.value = false
}

async function toggleActive(user) {
  try {
    await updateUser(user.id, { is_active: !user.is_active })
    loadUsers()
  } catch (e) {
    alert(e.response?.data?.detail || '操作失败')
  }
}

// 内置 admin 与当前登录账号不可删除（后端同样有校验，这里只做展示层拦截）
function canDelete(user) {
  return user.username !== 'admin' && user.username !== me.value.username
}

function deleteBlockedReason(user) {
  if (user.username === 'admin') return '内置管理员账号不可删除'
  if (user.username === me.value.username) return '当前登录账号不可删除'
  return ''
}

function openDeleteModal(user) {
  deleteTarget.value = user
  deleteMsg.value = ''
  showDeleteModal.value = true
}

function closeDeleteModal() {
  showDeleteModal.value = false
  deleteTarget.value = null
}

async function confirmDelete() {
  if (!deleteTarget.value) return
  deleteMsg.value = ''
  deleteSubmitting.value = true
  try {
    await deleteUser(deleteTarget.value.id)
    closeDeleteModal()
    await loadUsers()
  } catch (e) {
    deleteMsg.value = e.response?.data?.detail || '删除失败'
  }
  deleteSubmitting.value = false
}

// ---- 访问 IP 白名单（平台级；仅 admin 可改）----
const wl = ref({ enabled: false, entries: [], currentIp: '' })
const wlNew = ref({ cidr: '', remark: '' })
const wlSaving = ref(false)
const wlDirty = ref(false)
const wlMsg = ref('')
const wlMsgOk = ref(false)

const isLoopbackCurrent = computed(() => isLoopback(wl.value.currentIp))
// 界面上的「已在名单内」提示。真正的放行判定在后端，这里只为了让管理员少踩一次坑。
const currentIpInList = computed(() => cidrListContains(wl.value.currentIp, wl.value.entries))

function isLoopback(ip) {
  return ip === '::1' || String(ip).startsWith('127.')
}

function ipv4ToInt(ip) {
  const parts = String(ip).split('.')
  if (parts.length !== 4) return null
  let value = 0
  for (const part of parts) {
    const num = Number(part)
    if (!Number.isInteger(num) || num < 0 || num > 255) return null
    value = value * 256 + num
  }
  return value >>> 0
}

function cidrListContains(ip, entries) {
  if (!ip) return false
  return entries.some((entry) => cidrContains(entry.cidr, ip))
}

function cidrContains(cidr, ip) {
  if (!cidr) return false
  if (!cidr.includes('/')) return cidr === ip
  const [network, bitsRaw] = cidr.split('/')
  const bits = Number(bitsRaw)
  const net = ipv4ToInt(network)
  const addr = ipv4ToInt(ip)
  // IPv6 等场景退回精确匹配：前端不重复实现一套地址解析，正误以后端校验为准
  if (net === null || addr === null || !Number.isInteger(bits) || bits < 0 || bits > 32) return false
  const mask = bits === 0 ? 0 : (0xffffffff << (32 - bits)) >>> 0
  return ((net & mask) >>> 0) === ((addr & mask) >>> 0)
}

async function loadWhitelist() {
  if (!isAdmin.value) return
  try {
    const data = await getIpWhitelist()
    wl.value = { enabled: !!data.enabled, entries: data.entries || [], currentIp: data.current_ip || '' }
    wlDirty.value = false
  } catch (e) {
    wlMsg.value = e.response?.data?.detail || '白名单配置加载失败'
    wlMsgOk.value = false
  }
}

function addWhitelistEntry() {
  const cidr = wlNew.value.cidr.trim()
  if (!cidr) return
  if (wl.value.entries.some((entry) => entry.cidr === cidr)) {
    wlMsg.value = `「${cidr}」已在名单中`
    wlMsgOk.value = false
    return
  }
  wlMsg.value = ''
  // 这里不做合法性校验：后端用 ipaddress 统一校验并规范化（IPv4/IPv6 都支持），
  // 前端另写一套解析只会出现「前端放过、后端拒绝」的不一致
  wl.value.entries.push({ id: null, cidr, remark: wlNew.value.remark.trim(), created_by: '' })
  wlNew.value = { cidr: '', remark: '' }
  wlDirty.value = true
}

function addCurrentIpToWhitelist() {
  const ip = wl.value.currentIp
  if (!ip || currentIpInList.value) return
  wl.value.entries.push({ id: null, cidr: ip, remark: '当前访问 IP', created_by: '' })
  wlNew.value = { cidr: '', remark: '' }
  wlDirty.value = true
}

function removeWhitelistEntry(index) {
  wl.value.entries.splice(index, 1)
  wlDirty.value = true
}

function toggleWhitelist() {
  const next = !wl.value.enabled
  if (next) {
    if (wl.value.entries.length === 0) {
      wlMsg.value = '名单为空，无法启用：请先添加至少一条 IP 或网段'
      wlMsgOk.value = false
      return
    }
    const tip =
      `启用后只有名单内的 ${wl.value.entries.length} 条 IP / 网段能访问平台，\n` +
      '其他来源（包括登录页）一律显示「访问受限」。\n\n确认启用吗？'
    if (!confirm(tip)) return
  }
  wl.value.enabled = next
  wlDirty.value = true
  wlMsg.value = ''
}

async function saveWhitelist() {
  wlMsg.value = ''
  if (wl.value.enabled && wl.value.entries.length === 0) {
    wlMsg.value = '启用白名单前请至少添加一条 IP 或网段'
    wlMsgOk.value = false
    return
  }
  wlSaving.value = true
  try {
    const data = await saveIpWhitelist({
      enabled: wl.value.enabled,
      entries: wl.value.entries.map((entry) => ({ cidr: entry.cidr, remark: entry.remark || '' })),
    })
    // 用后端返回的规范化结果覆盖本地（单个 IP 会被补成 /32，网段会归到网络地址）
    wl.value = { enabled: !!data.enabled, entries: data.entries || [], currentIp: data.current_ip || '' }
    wlDirty.value = false
    wlMsg.value = data.enabled ? '已保存，白名单立即生效' : '已保存，当前不限制访问 IP'
    wlMsgOk.value = true
  } catch (e) {
    wlMsg.value = e.response?.data?.detail || '保存失败'
    wlMsgOk.value = false
  }
  wlSaving.value = false
}

onMounted(() => {
  loadMe().then(() => {
    loadUsers()
    loadWhitelist()
  })
})
</script>
