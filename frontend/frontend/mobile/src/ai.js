import { currentBaseUrl, getToken } from './store.js'

// 移动端 AI 请求：fetch 整包读取 + SSE 帧解析（不做流式 ReadableStream ——
// Android WebView 对流式 fetch 兼容性不稳定，且后端 AI 接口本就是一次性构建
// 完整文本后再分帧返回，整包读取即可可靠拿到全部内容）
// 帧协议与桌面端一致：data: {"t": "..."} / {"cached": true} / {"error": "..."} / data: [DONE]
export async function aiStream(path, body = {}, { onDelta, onCached, onError, onDone } = {}) {
  const base = currentBaseUrl().replace(/\/+$/, '')
  let res
  try {
    res = await fetch(`${base}${path}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${getToken() || ''}`,
      },
      body: JSON.stringify(body),
    })
  } catch (e) {
    onError && onError('网络连接失败，请检查服务器地址与网络')
    onDone && onDone()
    return
  }
  if (res.status === 401) {
    onError && onError('登录已过期，请重新登录')
    onDone && onDone()
    return
  }
  if (!res.ok) {
    onError && onError(`服务异常 (${res.status})，请确认平台已配置 AI 模型`)
    onDone && onDone()
    return
  }

  // 整包读取（fetch Response.text 为标准 API，各 WebView 均支持）
  let text
  try {
    text = await res.text()
  } catch (e) {
    onError && onError('读取响应失败，请重试')
    onDone && onDone()
    return
  }
  if (!text) {
    onDone && onDone()
    return
  }

  // 按 SSE 帧分隔解析
  const frames = text.split('\n\n')
  for (const frame of frames) {
    const line = frame.trim()
    if (!line.startsWith('data:')) continue
    const data = line.slice(5).trim()
    if (data === '[DONE]') {
      onDone && onDone()
      return
    }
    try {
      const obj = JSON.parse(data)
      if (obj.error) {
        onError && onError(obj.error)
        return
      }
      if (obj.cached) onCached && onCached()
      if (obj.t) onDelta && onDelta(obj.t)
    } catch (e) { /* 跳过坏帧 */ }
  }
  onDone && onDone()
}
