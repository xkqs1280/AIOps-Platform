// AI 辅助模块 API：axios 常规接口 + fetch SSE 流式封装
import axios from 'axios'

const http = axios.create({ baseURL: '/api/v1', timeout: 60000 })

http.interceptors.response.use(
  (res) => res.data,
  (err) => {
    if (err.response && err.response.status === 401) {
      if (!window.location.pathname.startsWith('/login')) window.location.href = '/login'
    }
    return Promise.reject(err)
  },
)

/**
 * 流式调用 AI 端点（POST + SSE 解析）。
 * @param {string} path 形如 /ai/chat
 * @param {object} body 请求体
 * @param {{onDelta?:Function,onError?:Function,onDone?:Function}} handlers
 */
export async function aiStream(path, body, handlers = {}) {
  let res
  try {
    res = await fetch('/api/v1' + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body || {}),
    })
  } catch (e) {
    handlers.onError && handlers.onError('网络异常：' + e.message)
    return
  }
  if (res.status === 401) {
    window.location.href = '/login'
    handlers.onError && handlers.onError('登录已失效')
    return
  }
  if (!res.ok || !res.body) {
    let msg = `HTTP ${res.status}`
    try {
      const j = await res.json()
      if (j.detail) msg = typeof j.detail === 'string' ? j.detail : msg
    } catch { /* ignore */ }
    handlers.onError && handlers.onError(msg)
    return
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  while (true) {
    let r
    try {
      r = await reader.read()
    } catch (e) {
      handlers.onError && handlers.onError('连接中断')
      return
    }
    if (r.done) break
    buf += decoder.decode(r.value, { stream: true })
    const frames = buf.split('\n\n')
    buf = frames.pop() || ''
    for (const frame of frames) {
      const line = frame.trim()
      if (!line.startsWith('data:')) continue
      const data = line.slice(5).trim()
      if (!data) continue
      if (data === '[DONE]') {
        handlers.onDone && handlers.onDone()
        return
      }
      try {
        const obj = JSON.parse(data)
        if (obj.error) {
          handlers.onError && handlers.onError(obj.error)
          return
        }
        if (obj.cached && handlers.onCached) handlers.onCached()
        if (obj.t) handlers.onDelta && handlers.onDelta(obj.t)
      } catch { /* 跳过坏帧 */ }
    }
  }
  handlers.onDone && handlers.onDone()
}

// 配置与连接测试（管理员）
export const getAiConfig = () => http.get('/ai/config')
export const saveAiConfig = (data) => http.post('/ai/config', data)
export const testAiConnection = () => http.post('/ai/test', {})

// 知识库（管理员）
export const aiKbDocs = () => http.get('/ai/kb/docs')
export const aiKbUpload = (formData) => http.post('/ai/kb/upload', formData, { timeout: 300000 })
export const aiKbDelete = (id) => http.delete(`/ai/kb/docs/${id}`)
export const aiKbSearch = (query) => http.post('/ai/kb/search', { query })

// 调用审计（管理员）
export const aiLogs = (params) => http.get('/ai/logs', { params })
