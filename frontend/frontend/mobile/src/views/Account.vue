<template>
  <div class="p-4 pb-8">
    <button @click="$router.back()" class="mb-3 flex items-center gap-1 text-sm text-ink-muted active:text-ink">← 返回</button>
    <h1 class="mb-3 text-lg font-bold text-ink-strong">账号设置</h1>

    <div v-if="me" class="mb-4 rounded-2xl border border-line bg-surface p-4">
      <div class="flex items-center gap-3">
        <div class="flex h-11 w-11 items-center justify-center rounded-full bg-cyan-500/15 text-lg font-bold text-cyan-400">
          {{ (me.username || 'A')[0].toUpperCase() }}
        </div>
        <div>
          <p class="text-sm font-semibold text-ink-strong">{{ me.username }}</p>
          <p class="text-xs text-ink-faint">{{ me.role === 'admin' ? '管理员' : '普通用户' }}</p>
        </div>
      </div>
    </div>

    <!-- 修改密码 -->
    <div class="rounded-2xl border border-line bg-surface p-4">
      <p class="mb-3 text-sm font-semibold text-ink">修改密码</p>
      <div class="space-y-2.5">
        <input v-model="oldPwd" type="password" placeholder="原密码" class="w-full rounded-xl border border-line bg-surface px-3 py-2.5 text-sm text-ink-strong outline-none placeholder:text-ink-faint focus:border-cyan-500" />
        <input v-model="newPwd" type="password" placeholder="新密码" class="w-full rounded-xl border border-line bg-surface px-3 py-2.5 text-sm text-ink-strong outline-none placeholder:text-ink-faint focus:border-cyan-500" />
        <input v-model="confirmPwd" type="password" placeholder="确认新密码" class="w-full rounded-xl border border-line bg-surface px-3 py-2.5 text-sm text-ink-strong outline-none placeholder:text-ink-faint focus:border-cyan-500" />
        <button @click="doChangePwd" :disabled="changing" class="w-full rounded-xl bg-cyan-600 py-2.5 text-sm font-medium text-white active:bg-cyan-500 disabled:opacity-50">
          {{ changing ? '提交中...' : '修改密码' }}
        </button>
        <p v-if="pwdMsg" class="text-center text-xs" :class="pwdOk ? 'text-emerald-400' : 'text-red-400'">{{ pwdMsg }}</p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getMe, changePassword } from '../api.js'

const me = ref(null)
const oldPwd = ref('')
const newPwd = ref('')
const confirmPwd = ref('')
const changing = ref(false)
const pwdMsg = ref('')
const pwdOk = ref(false)

onMounted(async () => {
  try { me.value = await getMe() } catch (e) { console.error(e) }
})

async function doChangePwd() {
  pwdMsg.value = ''
  if (!oldPwd.value || !newPwd.value) { pwdMsg.value = '请填写完整'; return }
  if (newPwd.value !== confirmPwd.value) { pwdMsg.value = '两次密码不一致'; return }
  changing.value = true
  try {
    await changePassword({ old_password: oldPwd.value, new_password: newPwd.value })
    pwdOk.value = true
    pwdMsg.value = '密码修改成功'
    oldPwd.value = newPwd.value = confirmPwd.value = ''
  } catch (e) {
    pwdOk.value = false
    pwdMsg.value = e?.response?.data?.detail || '修改失败'
  } finally {
    changing.value = false
  }
}
</script>
