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
                </div>
              </td>
            </tr>
            <tr v-if="users.length === 0">
              <td colspan="6" class="px-4 py-12 text-center text-ink-faint">暂无账号</td>
            </tr>
          </tbody>
        </table>
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
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { getMe, getUsers, createUser, updateUser, changePassword, logout } from '../api/index.js'

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

onMounted(() => {
  loadMe().then(() => loadUsers())
})
</script>
