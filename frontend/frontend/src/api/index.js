import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 15000,
  // 不全局锁定 Content-Type：JSON 请求由 axios 自动设为 application/json，
  // FormData 上传自动切换为 multipart/form-data（否则 file 字段收不到）
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
// 编辑设备页「测试连接」：探测 SSH/Telnet 凭据与 SNMP 是否可通（后端会并行探测，放宽超时）
export const testDeviceConnection = (data) => api.post('/devices/test-connection', data, { timeout: 60000 })
export const deleteDevice = (id) => api.delete(`/devices/${id}`)
export const batchDeleteDevices = (data) => api.post('/devices/batch-delete', data)
export const syncDevice = (id) => api.post(`/devices/${id}/sync`, null, { timeout: 120000 })
export const syncAllDevices = () => api.post('/devices/sync-all', null, { timeout: 600000 })
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

// 告警通知通道（钉钉 / 企业微信 / 飞书 / 自定义 Webhook）
export const getNotifyChannelMeta = () => api.get('/notify-channels/meta')
export const getNotifyChannels = () => api.get('/notify-channels')
export const createNotifyChannel = (data) => api.post('/notify-channels', data)
export const updateNotifyChannel = (id, data) => api.put(`/notify-channels/${id}`, data)
export const deleteNotifyChannel = (id) => api.delete(`/notify-channels/${id}`)
export const testNotifyChannel = (id) => api.post(`/notify-channels/${id}/test`, null, { timeout: 30000 })

// 设备依赖关系：模块已从 UI 取消（2026-09-10），依赖改由拓扑连线自动推导
// （见下方 getTopologyDependencies）。以下封装仅作扩展点备份，当前无页面引用。
export const getDeviceDependencies = () => api.get('/device-dependencies')
export const getDeviceDependenciesOf = (deviceId) => api.get(`/device-dependencies/device/${deviceId}`)
export const createDeviceDependency = (data) => api.post('/device-dependencies', data)
export const updateDeviceDependency = (id, data) => api.put(`/device-dependencies/${id}`, data)
export const deleteDeviceDependency = (id) => api.delete(`/device-dependencies/${id}`)

// 拓扑
export const getTopology = () => api.get('/topology')
export const getTopologyLinks = () => api.get('/topology/links')
export const createTopologyLink = (data) => api.post('/topology/links', data)
export const deleteTopologyLink = (id) => api.delete(`/topology/links/${id}`)
// 自动推导的设备依赖（用于告警拓扑依赖抑制，也可在拓扑页查看）
export const getTopologyDependencies = () => api.get('/topology/dependencies')

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
// 核查会逐台 SSH 采集运行态命令，远超默认 15s（当前无调用方，防后续接上又踩坑）
export const runComplianceCheck = () => api.post('/compliance/check', null, { timeout: 600000 })

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
export const deleteUser = (id) => api.delete(`/auth/users/${id}`)
export const changePassword = (data) => api.post('/auth/change-password', data)

// 设备日志中心（内置 syslog UDP 接收 → device_logs 留存）
export const getDeviceLogs = (params) => api.get('/device-logs', { params })
export const getDeviceLogStats = (params) => api.get('/device-logs/stats', { params })
export const getDeviceLogFilters = (params) => api.get('/device-logs/filters', { params })
export const getDeviceLogReceiver = () => api.get('/device-logs/receiver')
// 开启/关闭设备日志接收（停收时立即释放 UDP 端口；存量日志不受影响）
export const setDeviceLogReceiver = (enabled) => api.put('/device-logs/receiver', { enabled })
// 导出按筛选条件流式拼 CSV，条数多时可能超过默认 15s（后端有 5 万条上限）
export const exportDeviceLogs = (params) => api.get('/device-logs/export', { params, responseType: 'blob', timeout: 120000 })
// 日志主机（loghost）下发：真实改动设备配置，属高危运维操作
export const getLoghostCandidates = () => api.get('/device-logs/loghost/candidates')
export const previewLoghost = (data) => api.post('/device-logs/loghost/preview', data)
// 每台设备要连 SSH 下发 7 条命令并前后各取一次快照，单台最长 2 分钟；
// 前端按 ≤30 台分块提交，单块最坏情况 ≈ (30/4) × 15s ≈ 2 分钟，5 分钟超时留足余量
export const applyLoghost = (data) => api.post('/device-logs/loghost/apply', data, { timeout: 300000 })
export const rollbackLoghost = (data) => api.post('/device-logs/loghost/rollback', data, { timeout: 300000 })
export const getLoghostStatus = () => api.get('/device-logs/loghost/status')

// 系统设置
export const getMailSetting = () => api.get('/settings/mail')
export const saveMailSetting = (data) => api.post('/settings/mail', data)
export const getAuditLogs = (params) => api.get('/settings/audit-logs', { params })
// 平台外观设置（监控大屏标题可自定义为「某某单位网络监控平台」）
// site_name 为空串 = 未自定义，前端回退到内置默认文案
export const getPlatformSetting = () => api.get('/settings/platform')
export const savePlatformSetting = (siteName) => api.put('/settings/platform', { site_name: siteName })

// 访问控制 · 平台访问 IP 白名单（账号管理页维护；启用后整站拦截）
// 读取会一并返回 current_ip（平台看到的本次来源 IP），供界面提示与一键加入
export const getIpWhitelist = () => api.get('/access-control/ip-whitelist')
// 全量保存：请求体即权威列表（后端整表替换，并校验不能把自己锁在门外）
export const saveIpWhitelist = (data) => api.put('/access-control/ip-whitelist', data)

// 系统升级（一键升级模块）
export const getSystemVersion = () => api.get('/system/version')
export const startUpgrade = (file) => {
  const fd = new FormData()
  fd.append('file', file)
  return api.post('/system/upgrade', fd, {
    timeout: 120000,
    // 覆盖实例全局的 application/json：设 undefined 让 axios/浏览器自动生成
    // multipart/form-data + boundary，否则后端收不到 file（FastAPI 422 Field required）
    headers: { 'Content-Type': undefined },
  })
}
export const getUpgradeStatus = () => api.get('/system/upgrade/status')
export const requestRollback = () => api.post('/system/upgrade/rollback')

export default api
