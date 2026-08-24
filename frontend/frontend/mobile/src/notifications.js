// 告警系统通知服务：轮询新告警并通过 Capacitor LocalNotifications 弹系统通知
import { LocalNotifications } from '@capacitor/local-notifications'
import { getAlerts } from './api.js'
import { getToken, getServer } from './store.js'

// 已通知过的告警 ID（会话级 + 持久化，避免重复弹）
const NOTIFIED_KEY = 'aiops_mobile_notified_ids'
let polling = null
let running = false

function loadNotified() {
  try {
    return JSON.parse(localStorage.getItem(NOTIFIED_KEY) || '[]')
  } catch { return [] }
}

function saveNotified(ids) {
  // 只保留最近 200 个，避免 localStorage 膨胀
  localStorage.setItem(NOTIFIED_KEY, JSON.stringify(ids.slice(-200)))
}

const SEV_LABEL = { critical: '严重', major: '重要', minor: '次要', warning: '警告' }

// 请求系统通知权限（Android 13+ 必须，其余版本自动 true）
export async function ensureNotificationPermission() {
  try {
    const perm = await LocalNotifications.checkPermissions()
    if (perm.display !== 'granted') {
      const req = await LocalNotifications.requestPermissions()
      return req.display === 'granted'
    }
    return true
  } catch (e) {
    console.error('通知权限请求失败:', e)
    return false
  }
}

// 创建通知 channel（Android 8+ 必需）
let channelCreated = false
export async function ensureNotificationChannel() {
  if (channelCreated) return
  try {
    await LocalNotifications.createChannel({
      id: 'aiops-alerts',
      name: 'AIOps 告警',
      description: '设备故障与告警通知',
      importance: 5, // IMPORTANCE_HIGH
      visibility: 1, // VISIBILITY_PUBLIC
      sound: 'default',
      vibration: true,
    })
    channelCreated = true
  } catch (e) {
    console.error('创建通知 channel 失败:', e)
  }
}

// 检查并发送新告警通知
async function checkAndNotify() {
  if (!getToken()) return
  try {
    const res = await getAlerts({ status: 'active', page_size: 50 })
    const items = res?.items || res?.data || []
    if (!items.length) return
    const notified = new Set(loadNotified())
    const newOnes = items.filter((a) => a && a.id != null && !notified.has(String(a.id)))
    // 只通知严重/重要级别（避免刷屏），其余静默记录
    const toNotify = newOnes.filter((a) => ['critical', 'major'].includes(a.severity))
    // 确保 channel 已创建（防止首次 schedule 时 channel 还没建好）
    if (toNotify.length) await ensureNotificationChannel()
    for (const a of toNotify.slice(0, 5)) {
      const nid = Number(String(a.id).slice(-9)) % 2147483647 || 1
      const title = `${SEV_LABEL[a.severity] || a.severity}告警`
      const body = `${a.device_name || ''} ${a.device_ip ? '(' + a.device_ip + ')' : ''}\n${a.message || a.alert_name || ''}`
      try {
        // 同 ID 旧通知先取消（让新内容覆盖）
        await LocalNotifications.cancel({ notifications: [{ id: nid }] }).catch(() => {})
        await LocalNotifications.schedule({
          notifications: [{
            id: nid,
            title,
            body,
            smallIcon: 'ic_stat_icon',
            channelId: 'aiops-alerts',
            autoCancel: true,
          }],
        })
        console.log('通知已发送:', nid, title)
      } catch (e) {
        console.error('通知发送失败:', e)
      }
    }
    // 记录所有已见 ID（含不通知的），下次不再弹
    for (const a of items) {
      if (a && a.id != null) notified.add(String(a.id))
    }
    saveNotified([...notified])
  } catch (e) {
    // 静默失败，轮询继续
    console.error('告警轮询失败:', e)
  }
}

// 启动轮询（登录后调用）
export function startAlertPolling(intervalMs = 30000) {
  if (running) return
  running = true
  // 权限 + channel 准备完毕再启动轮询
  Promise.resolve().then(async () => {
    const ok = await ensureNotificationPermission()
    if (!ok) console.warn('通知权限未授予，仅静默记录告警')
    await ensureNotificationChannel()
  })
  // 立即跑一次
  checkAndNotify()
  polling = setInterval(checkAndNotify, intervalMs)
}

// 停止轮询（退出登录调用）
export function stopAlertPolling() {
  running = false
  if (polling) {
    clearInterval(polling)
    polling = null
  }
}
