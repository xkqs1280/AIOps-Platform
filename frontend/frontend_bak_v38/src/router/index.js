import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('../views/Login.vue'),
    meta: { title: '登录' },
  },
  {
    path: '/',
    name: 'Dashboard',
    component: () => import('../views/Dashboard.vue'),
    meta: { title: '监控大屏' },
  },
  {
    path: '/devices',
    name: 'Devices',
    component: () => import('../views/Devices.vue'),
    meta: { title: '设备管理' },
  },
  {
    path: '/devices/:id',
    name: 'DeviceDetail',
    component: () => import('../views/DeviceDetail.vue'),
    meta: { title: '设备详情' },
  },
  {
    path: '/alerts',
    name: 'Alerts',
    component: () => import('../views/Alerts.vue'),
    meta: { title: '告警管理' },
  },
  {
    path: '/topology',
    name: 'Topology',
    component: () => import('../views/Topology.vue'),
    meta: { title: '拓扑发现' },
  },
  {
    path: '/lifecycle',
    name: 'Lifecycle',
    component: () => import('../views/Lifecycle.vue'),
    meta: { title: '生命周期管理' },
  },
  {
    path: '/security',
    name: 'SecurityDashboard',
    component: () => import('../views/SecurityDashboard.vue'),
    meta: { title: '安全监控面板' },
  },
  {
    path: '/compliance',
    name: 'Compliance',
    component: () => import('../views/Compliance.vue'),
    meta: { title: '等保合规管理' },
  },
  {
    path: '/config-backup',
    name: 'ConfigBackup',
    component: () => import('../views/ConfigBackup.vue'),
    meta: { title: '配置备份' },
  },
  {
    path: '/inspection',
    name: 'Inspection',
    component: () => import('../views/Inspection.vue'),
    meta: { title: 'H3C 设备巡检' },
  },
  {
    path: '/business-monitor',
    name: 'BusinessMonitor',
    component: () => import('../views/BusinessMonitor.vue'),
    meta: { title: '重要业务监控' },
  },
  // 系统设置（分组子路由）
  {
    path: '/settings',
    name: 'Settings',
    component: () => import('../views/Settings.vue'),
    redirect: '/settings/alert-rules',
    meta: { title: '系统设置' },
    children: [
      {
        path: 'mail',
        name: 'MailSettings',
        component: () => import('../views/MailSettings.vue'),
        meta: { title: '邮件告警' },
      },
      {
        path: 'alert-rules',
        name: 'AlertRules',
        component: () => import('../views/AlertRules.vue'),
        meta: { title: '告警规则' },
      },
      {
        path: 'account',
        name: 'Account',
        component: () => import('../views/Account.vue'),
        meta: { title: '账号管理' },
      },
      {
        path: 'license',
        name: 'License',
        component: () => import('../views/License.vue'),
        meta: { title: '授权管理' },
      },
      {
        path: 'audit-logs',
        name: 'AuditLogs',
        component: () => import('../views/AuditLogs.vue'),
        meta: { title: '审计日志' },
      },
    ],
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} - AIOps` : 'AIOps 智能运维托管平台'
})

// 全局路由守卫：未登录访问受限页面跳转登录页（纵深防御，接口侧仍有鉴权）
router.beforeEach(async (to) => {
  if (to.path === '/login') return true
  try {
    const res = await fetch('/api/v1/auth/me', { credentials: 'same-origin' })
    if (res.ok) return true
  } catch (e) {
    // 网络异常时放行，由页面请求自行处理（避免断网被踢到登录页）
    return true
  }
  return { path: '/login', query: { redirect: to.fullPath } }
})

export default router
