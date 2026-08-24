import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  { path: '/login', name: 'Login', component: () => import('./views/Login.vue'), meta: { title: '登录' } },
  { path: '/', name: 'Dashboard', component: () => import('./views/Dashboard.vue'), meta: { title: '监控' } },
  { path: '/devices', name: 'Devices', component: () => import('./views/Devices.vue'), meta: { title: '设备' } },
  { path: '/devices/:id', name: 'DeviceDetail', component: () => import('./views/DeviceDetail.vue'), meta: { title: '设备详情' } },
  { path: '/alerts', name: 'Alerts', component: () => import('./views/Alerts.vue'), meta: { title: '告警' } },
  { path: '/topology', name: 'Topology', component: () => import('./views/Topology.vue'), meta: { title: '拓扑' } },
  { path: '/more', name: 'More', component: () => import('./views/More.vue'), meta: { title: '更多' } },
  { path: '/lifecycle', name: 'Lifecycle', component: () => import('./views/Lifecycle.vue'), meta: { title: '生命周期' } },
  { path: '/compliance', name: 'Compliance', component: () => import('./views/Compliance.vue'), meta: { title: '合规' } },
  { path: '/security', name: 'Security', component: () => import('./views/Security.vue'), meta: { title: '安全' } },
  { path: '/inspection', name: 'Inspection', component: () => import('./views/Inspection.vue'), meta: { title: '巡检' } },
  { path: '/account', name: 'Account', component: () => import('./views/Account.vue'), meta: { title: '账号' } },
  { path: '/system-upgrade', name: 'SystemUpgrade', component: () => import('./views/SystemUpgrade.vue'), meta: { title: '系统升级' } },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} - AIOps` : 'AIOps'
})

export default router
