<template>
  <div class="min-h-full p-6 animate-in">
    <div class="mb-6">
      <h1 class="text-2xl font-bold text-ink-strong">审计日志</h1>
      <p class="mt-1 text-sm text-ink-muted">记录登录、设备变更、用户管理等敏感操作的完整轨迹</p>
    </div>

    <div class="mb-4 flex flex-wrap items-center gap-3">
      <select
        v-model="filterModule"
        @change="resetAndLoad"
        class="rounded-lg border border-line bg-surface-2 px-3 py-1.5 text-sm text-ink focus:border-cyan-500 focus:outline-none"
      >
        <option value="">全部模块</option>
        <option value="auth">登录认证</option>
        <option value="user">用户管理</option>
        <option value="device">设备管理</option>
        <option value="license">授权管理</option>
        <option value="mail">邮件设置</option>
        <option value="upgrade">系统升级</option>
      </select>
      <input
        v-model="filterKeyword"
        placeholder="搜索用户 / 操作内容..."
        class="input max-w-64"
        @keyup.enter="resetAndLoad"
      />
      <button
        @click="resetAndLoad"
        class="rounded-lg bg-cyan-600/20 px-3 py-1.5 text-sm text-cyan-300 border border-cyan-600/40 hover:bg-cyan-600/30"
      >查询</button>
      <button
        @click="fetchLogs"
        class="rounded-lg bg-surface-2 px-3 py-1.5 text-sm text-ink-muted border border-line hover:bg-hover"
      >刷新</button>
    </div>

    <div class="rounded-xl border border-line bg-surface/50">
      <table class="w-full text-sm">
        <thead>
          <tr class="border-b border-line text-left text-xs text-ink-faint">
            <th class="px-4 py-3 font-medium">时间</th>
            <th class="px-4 py-3 font-medium">用户</th>
            <th class="px-4 py-3 font-medium">角色</th>
            <th class="px-4 py-3 font-medium">模块</th>
            <th class="px-4 py-3 font-medium">操作</th>
            <th class="px-4 py-3 font-medium">内容</th>
            <th class="px-4 py-3 font-medium">IP</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="log in logs" :key="log.id" class="border-b border-line/60 hover:bg-hover/30">
            <td class="px-4 py-2.5 text-xs text-ink-muted whitespace-nowrap">{{ formatTime(log.created_at) }}</td>
            <td class="px-4 py-2.5 font-medium text-ink">{{ log.user }}</td>
            <td class="px-4 py-2.5">
              <span
                class="inline-flex items-center rounded border px-1.5 py-0.5 text-xs font-medium"
                :class="log.role === 'admin' ? 'border-red-600/30 bg-red-600/15 text-red-400' : log.role === 'operator' ? 'border-amber-600/30 bg-amber-600/15 text-amber-400' : 'border-cyan-600/30 bg-cyan-600/15 text-cyan-400'"
              >{{ log.role }}</span>
            </td>
            <td class="px-4 py-2.5 text-ink-muted">{{ moduleLabel(log.module) }}</td>
            <td class="px-4 py-2.5">
              <span
                class="inline-flex items-center rounded border px-1.5 py-0.5 text-xs font-medium"
                :class="actionClass(log.action)"
              >{{ log.action }}</span>
            </td>
            <td class="px-4 py-2.5 text-xs text-ink-muted max-w-md truncate" :title="log.detail">{{ log.detail || '-' }}</td>
            <td class="px-4 py-2.5 text-xs text-ink-faint font-mono">{{ log.ip || '-' }}</td>
          </tr>
          <tr v-if="logs.length === 0">
            <td colspan="7" class="px-4 py-10 text-center text-sm text-ink-faint">暂无审计日志</td>
          </tr>
        </tbody>
      </table>

      <div class="flex items-center justify-between px-4 py-3 border-t border-line">
        <span class="text-xs text-ink-faint">共 {{ total }} 条记录</span>
        <div class="flex items-center gap-3 text-sm">
          <span class="flex items-center gap-1 text-xs text-ink-faint">
            每页
            <select
              v-model="pageSize"
              @change="page = 1; fetchLogs()"
              class="rounded-lg border border-line bg-surface-2 px-2 py-1 text-sm text-ink focus:outline-none"
            >
              <option :value="10">10</option>
              <option :value="20">20</option>
              <option :value="50">50</option>
            </select>
            条
          </span>
          <button
            @click="page--; fetchLogs()"
            :disabled="page <= 1"
            class="rounded-lg border border-line bg-surface-2 px-3 py-1 text-ink-muted hover:bg-hover disabled:opacity-40 disabled:cursor-not-allowed"
          >上一页</button>
          <span class="text-xs text-ink-muted">第 {{ page }} / {{ totalPages }} 页</span>
          <button
            @click="page++; fetchLogs()"
            :disabled="page >= totalPages"
            class="rounded-lg border border-line bg-surface-2 px-3 py-1 text-ink-muted hover:bg-hover disabled:opacity-40 disabled:cursor-not-allowed"
          >下一页</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { getAuditLogs } from '../api/index.js'

const logs = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filterModule = ref('')
const filterKeyword = ref('')
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))

const moduleMap = {
  auth: '登录认证',
  user: '用户管理',
  device: '设备管理',
  license: '授权管理',
  mail: '邮件设置',
  upgrade: '系统升级',
}
function moduleLabel(m) {
  return moduleMap[m] || m
}
function actionClass(action) {
  const danger = ['delete', 'batch_delete', 'login_failed']
  const warn = ['update', 'logout', 'change_password']
  if (danger.includes(action)) return 'border-red-600/30 bg-red-600/15 text-red-400'
  if (warn.includes(action)) return 'border-amber-600/30 bg-amber-600/15 text-amber-400'
  return 'border-cyan-600/30 bg-cyan-600/15 text-cyan-400'
}
function formatTime(v) {
  if (!v) return '-'
  return String(v).slice(0, 19).replace('T', ' ')
}
function resetAndLoad() {
  page.value = 1
  fetchLogs()
}
async function fetchLogs() {
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filterModule.value) params.module = filterModule.value
    if (filterKeyword.value.trim()) params.keyword = filterKeyword.value.trim()
    const data = await getAuditLogs(params)
    logs.value = data.items || []
    total.value = data.total || 0
    if (page.value > totalPages.value) {
      page.value = totalPages.value
      return fetchLogs()
    }
  } catch (e) {
    console.error('Audit logs fetch error:', e)
  }
}
onMounted(fetchLogs)
</script>
