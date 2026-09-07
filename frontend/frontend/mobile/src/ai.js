import { currentBaseUrl, getToken } from './store.js'

// 移动端 AI SSE 流式请求：fetch + ReadableStream 解析（axios 不支持流式）
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
  if (!res.ok || !res.body) {
    onError && onError(`服务异常 (${res.status})，请确认平台已配置 AI 模型`)
    onDone && onDone()
    return
  }

  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  while (true) {
    let chunk
    try {
      chunk = await reader.read()
    } catch (e) {
      break
    }
    if (chunk.done) break
    buf += dec.decode(chunk, { stream: true })
    const frames = buf.split('\n\n')
    buf = frames.pop() || ''
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
  }
  onDone && onDone()
}
