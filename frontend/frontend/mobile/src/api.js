import axios from 'axios'
import { currentBaseUrl, getToken, setToken } from './store.js'
import tofuAdapter, { isTofuAdapterActive } from './tofuAdapter.js'
import { resolveTofu } from './tofu.js'

// 移动端 API：baseURL 动态指向用户配置的服务器，携带 Bearer token（跨域安全）。
// 原生端走 TOFU HTTPS 通道（任意自签名平台，首次指纹确认后免证书），Web 端回退默认 XHR。
const api = axios.create({
  baseURL: currentBaseUrl(),
  timeout: 20000,
  adapter: isTofuAdapterActive() ? tofuAdapter : undefined,
  // 不全局锁定 Content-Type：JSON 请求由 axios 自动设为 application/json，
  // FormData 上传自动切换为 multipart/form-data（否则 file 字段收不到）
})

// 每次请求前：baseURL 可能因切换服务器而变化，token 从存储实时读取
api.interceptors.request.use((config) => {
  config.baseURL = currentBaseUrl()
  const token = getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

let onUnauthorized = null
export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn
}

// 登录/登出自身的 401（口令错误等）不代表会话过期，不得触发全局跳登录
const isAuthEndpoint = (url) => /\/auth\/(login|logout)$/.test(String(url || ''))

api.interceptors.response.use(
  (res) => res.data,
  async (err) => {
    // 401：token 失效 → 清 token 回登录（登录端点自身的 401 除外）
    if (err.response && err.response.status === 401 && !isAuthEndpoint(err.config && err.config.url)) {
      setToken('')
      if (onUnauthorized) onUnauthorized()
      return Promise.reject(err)
    }
    // TOFU：仅「首次连接」与「证书指纹变化」弹确认（全局 TofuDialog）；
    // 网络不可达 / 证书过期等其它错误直接透传，绝不误弹"指纹变化"诱导信任
    const tofuCode = err.tofu && err.tofu.code
    if (
      (tofuCode === 'TOFU_FIRST_USE' || tofuCode === 'TOFU_MISMATCH') &&
      !err.config?._tofuRetried
    ) {
      const accepted = await resolveTofu(err.tofu)
      if (accepted) {
        err.config._tofuRetried = true
        return api.request(err.config)
      }
    }
    return Promise.reject(err)
  },
)

// ---------- 与桌面端一致的 API 封装 ----------
// 设备管理
export const getDevices = (params) => api.get('/devices', { params })
export const getDevice = (id) => api.get(`/devices/${id}`)
export const getDeviceComponents = (id) => api.get(`/devices/${id}/components`)
export const getDeviceInterfaces = (id, top = 10) => api.get(`/devices/${id}/interfaces`, { params: { top }, timeout: 60000 })
export const syncDevice = (id) => api.post(`/devices/${id}/sync`)

// 告警
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

// 大屏
export const getDashboardOverview = () => api.get('/dashboard/overview')
export const getCpuRanking = () => api.get('/dashboard/cpu-ranking')
export const getMemoryRanking = () => api.get('/dashboard/memory-ranking')
export const getBandwidthRanking = () => api.get('/dashboard/bandwidth-ranking', { timeout: 60000 })
export const getRecentAlerts = () => api.get('/dashboard/recent-alerts')

// 生命周期
export const getLifecycleReminders = () => api.get('/lifecycle/reminders')

// 安全监控
export const getExternalThreatLatest = () => api.get('/security/external/latest')
export const getExternalThreatHistory = (params) => api.get('/security/external/history', { params })

// 等保合规
export const getComplianceStatus = () => api.get('/compliance/status')
export const getComplianceScore = (deviceId) => api.get(`/compliance/score/${deviceId}`)

// 指标
export const getMetricHistory = (params) => api.get('/metrics/history', { params })
export const getLatestMetrics = (deviceId) => api.get('/metrics/latest', { params: { device_id: deviceId } })

// 巡检
export const getInspectionTasks = (params) => api.get('/inspections', { params })
export const getInspectionTask = (id) => api.get(`/inspections/${id}`)

// 重要业务监控
export const getBizGroups = () => api.get('/business-monitor/groups')
export const getBizSummary = () => api.get('/business-monitor/summary')
export const getBizTerminals = (params) => api.get('/business-monitor/terminals', { params })
export const probeBizTerminal = (id) => api.post(`/business-monitor/terminals/${id}/probe`)
export const probeBizAll = () => api.post('/business-monitor/terminals/probe-all')
export const getBizAlerts = (params) => api.get('/business-monitor/alerts', { params })
export const clearBizAlerts = () => api.delete('/business-monitor/alerts')
export const deleteBizAlert = (id) => api.delete(`/business-monitor/alerts/${id}`)

// 授权
export const getLicenseStatus = () => api.get('/license/status')
export const getLicenseFingerprint = () => api.get('/license/fingerprint')
export const activateLicense = (licenseCode) => api.post('/license/activate', { license_code: licenseCode })

// 账号
export const getMe = () => api.get('/auth/me')
export const getUsers = () => api.get('/auth/users')
export const changePassword = (data) => api.post('/auth/change-password', data)
export const login = (data) => api.post('/auth/login', data)
export const logout = () => api.post('/auth/logout')

// 系统设置
export const getMailSetting = () => api.get('/settings/mail')
export const getAuditLogs = (params) => api.get('/settings/audit-logs', { params })

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
