import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
})

let redirectingToLogin = false
function goLogin() {
  if (redirectingToLogin || window.location.pathname.startsWith('/login')) return
  redirectingToLogin = true
  window.location.href = '/login'
}

api.interceptors.response.use(
  (res) => res.data,
  (err) => {
    // 登录失效 / 未登录（会话过期、token 无效）：跳转登录页重新登录
    if (err.response && err.response.status === 401) {
      goLogin()
      return Promise.reject(err)
    }
    // 平台授权锁定（未激活 / 测试版到期）：跳转授权页
    if (err.response && err.response.data && err.response.data.code === 403002) {
      if (!window.location.pathname.startsWith('/settings/license')) {
        window.location.href = '/settings/license'
      }
    }
    console.error('API Error:', err)
    return Promise.reject(err)
  },
)

// 设备管理
export const getDevices = (params) => api.get('/devices', { params })
export const exportDevices = (params) => api.get('/devices/export', { params, responseType: 'blob' })
export const getDevice = (id) => api.get(`/devices/${id}`)
export const getDeviceComponents = (id) => api.get(`/devices/${id}/components`)
export const getDeviceInterfaces = (id, top = 10) => api.get(`/devices/${id}/interfaces`, { params: { top }, timeout: 60000 })
export const createDevice = (data) => api.post('/devices', data)
export const updateDevice = (id, data) => api.put(`/devices/${id}`, data)
export const deleteDevice = (id) => api.delete(`/devices/${id}`)
export const batchDeleteDevices = (data) => api.post('/devices/batch-delete', data)
export const syncDevice = (id) => api.post(`/devices/${id}/sync`)
export const discoverDevices = (data) => api.post('/devices/discover', data, { timeout: 60000 })
export const batchCreateDevices = (data) => api.post('/devices/batch', data)

// 告警管理
export const getAlerts = (params) => api.get('/alerts', { params })
export const deleteAlert = (id) => api.delete(`/alerts/${id}`)
export const clearAlerts = () => api.delete('/alerts')
export const getAlertStats = () => api.get('/alerts/stats')

// 告警规则
export const getAlertRules = () => api.get('/alert-rules')
export const createAlertRule = (data) => api.post('/alert-rules', data)
export const updateAlertRule = (id, data) => api.put(`/alert-rules/${id}`, data)
export const deleteAlertRule = (id) => api.delete(`/alert-rules/${id}`)

// 拓扑
export const getTopology = () => api.get('/topology')
export const getTopologyLinks = () => api.get('/topology/links')
export const createTopologyLink = (data) => api.post('/topology/links', data)
export const deleteTopologyLink = (id) => api.delete(`/topology/links/${id}`)

// 大屏 Dashboard
export const getDashboardOverview = () => api.get('/dashboard/overview')
export const getCpuRanking = () => api.get('/dashboard/cpu-ranking')
export const getMemoryRanking = () => api.get('/dashboard/memory-ranking')
export const getBandwidthRanking = () => api.get('/dashboard/bandwidth-ranking', { timeout: 60000 })
export const getDashboardLifecycle = () => api.get('/dashboard/lifecycle')
export const getRecentAlerts = () => api.get('/dashboard/recent-alerts')

// 设备生命周期
export const getLifecycleReminders = () => api.get('/lifecycle/reminders')
export const getLifecycleDb = () => api.get('/lifecycle/db')
export const createLifecycleDb = (data) => api.post('/lifecycle/db', data)
export const createLifecycleDbBatch = (params) => api.post('/lifecycle/db/batch', null, { params })
export const updateLifecycleDb = (id, data) => api.put(`/lifecycle/db/${id}`, data)
export const deleteLifecycleDb = (id) => api.delete(`/lifecycle/db/${id}`)
export const seedLifecycleDb = () => api.post('/lifecycle/seed')

// 安全监控（外部实时威胁态势）
export const getExternalThreatLatest = () => api.get('/security/external/latest')
export const getExternalThreatHistory = (params) => api.get('/security/external/history', { params })

// 等保合规
export const getComplianceStatus = () => api.get('/compliance/status')
export const getComplianceScore = (deviceId) => api.get(`/compliance/score/${deviceId}`)
export const runComplianceCheck = () => api.post('/compliance/check')

// 指标时序（真实 SNMP 采集）
export const getMetricHistory = (params) => api.get('/metrics/history', { params })
export const getLatestMetrics = (deviceId) => api.get('/metrics/latest', { params: { device_id: deviceId } })

// H3C 设备巡检
export const getInspectionTasks = (params) => api.get('/inspections', { params })
export const getInspectionTask = (id) => api.get(`/inspections/${id}`)
export const createInspectionTask = (data) => api.post('/inspections', data)
export const downloadInspectionExcel = (id) => api.get(`/inspections/${id}/download/excel`, { responseType: 'blob' })
export const downloadInspectionWord = (id) => api.get(`/inspections/${id}/download/word`, { responseType: 'blob' })

// 授权管理
export const getLicenseStatus = () => api.get('/license/status')
export const getLicenseFingerprint = () => api.get('/license/fingerprint')
export const activateLicense = (licenseCode) => api.post('/license/activate', { license_code: licenseCode })

// 账号管理
export const getMe = () => api.get('/auth/me')
export const getUsers = () => api.get('/auth/users')
export const createUser = (data) => api.post('/auth/users', data)
export const login = (data) => api.post('/auth/login', data)
export const logout = () => api.post('/auth/logout')
export const updateUser = (id, data) => api.patch(`/auth/users/${id}`, data)
export const changePassword = (data) => api.post('/auth/change-password', data)

// 系统设置
export const getMailSetting = () => api.get('/settings/mail')
export const saveMailSetting = (data) => api.post('/settings/mail', data)
export const getAuditLogs = (params) => api.get('/settings/audit-logs', { params })

export default api
