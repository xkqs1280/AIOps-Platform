// axios 自定义 adapter：原生端将 API 请求改走 AiopConnector（TOFU HTTPS 通道），
// 使任意自签名平台证书在"首次指纹确认"后即可连接；Web/浏览器环境自动回退默认 XHR adapter。
import { isNative, nativeConnector, nativeRequest } from './tofu.js'

// 移动端升级包等大文件经原生桥只能 base64 传递，设上限；超过请用电脑浏览器执行平台升级
const FILE_SIZE_LIMIT = 6 * 1024 * 1024 // 6MB

export function isTofuAdapterActive() {
  return isNative && !!nativeConnector()
}

function flattenHeaders(headers) {
  const out = {}
  try {
    if (headers && typeof headers.toJSON === 'function') {
      const h = headers.toJSON()
      for (const k of Object.keys(h)) {
        const v = h[k]
        if (v !== undefined && v !== null) out[k] = String(v)
      }
    } else if (headers) {
      for (const k of Object.keys(headers)) {
        const v = headers[k]
        if (v !== undefined && v !== null) out[k] = String(v)
      }
    }
  } catch (e) {
    /* ignore */
  }
  return out
}

function buildFullUrl(config) {
  let url = config.url || ''
  if (/^https?:\/\//i.test(url)) return url
  let base = config.baseURL || ''
  if (base && base.endsWith('/')) base = base.slice(0, -1)
  if (!url.startsWith('/') && base) url = '/' + url
  return base + url
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    if (file.size > FILE_SIZE_LIMIT) {
      reject(new Error('文件超过 6MB，移动端不支持上传，请用电脑浏览器执行平台升级'))
      return
    }
    const reader = new FileReader()
    reader.onload = () => {
      const result = String(reader.result || '')
      // data:application/octet-stream;base64,XXXX  → 去掉前缀
      const idx = result.indexOf(',')
      resolve(idx >= 0 ? result.slice(idx + 1) : result)
    }
    reader.onerror = () => reject(new Error('读取文件失败'))
    reader.readAsDataURL(file)
  })
}

function strToBase64(s) {
  // 浏览器环境用 TextEncoder → 字节 → base64（避免 btoa 对中文抛错）
  const bytes = new TextEncoder().encode(String(s))
  let bin = ''
  bytes.forEach((b) => (bin += String.fromCharCode(b)))
  return btoa(bin)
}

/** 把 axios 的 data 转成原生可传参（JSON 文本 / multipart parts） */
async function encodeBody(data) {
  if (data === undefined || data === null) return { kind: 'none' }
  if (typeof data === 'string') return { kind: 'text', body: data }

  if (typeof FormData !== 'undefined' && data instanceof FormData) {
    const parts = []
    const boundary = '----AiopBoundary' + Math.random().toString(36).slice(2) + Date.now().toString(36)
    for (const [name, value] of data.entries()) {
      if (value && typeof value === 'object' && typeof value.size === 'number' && typeof value.name === 'string') {
        // File / Blob
        const b64 = await fileToBase64(value)
        parts.push({
          name,
          filename: value.name || 'file',
          type: value.type || 'application/octet-stream',
          data: b64,
        })
      } else {
        parts.push({ name, filename: '', type: 'text/plain', data: strToBase64(value) })
      }
    }
    return { kind: 'multipart', parts, boundary }
  }

  // 其它对象：JSON
  try {
    return { kind: 'text', body: JSON.stringify(data) }
  } catch (e) {
    return { kind: 'none' }
  }
}

function makeTofuError(code, info, config) {
  const msg =
    code === 'TOFU_FIRST_USE'
      ? '首次连接该平台，需要确认服务器证书指纹'
      : code === 'TOFU_MISMATCH'
        ? '服务器证书指纹与已信任的不一致'
        : info.message || '网络连接失败'
  const err = new Error(msg)
  err.tofu = { code, ...info }
  err.config = config
  err.isTofuError = true
  return err
}

export default function tofuAdapter(config) {
  return new Promise((resolve, reject) => {
    ;(async () => {
      try {
        const url = buildFullUrl(config)
        const headers = flattenHeaders(config.headers)
        const { kind, body, parts, boundary } = await encodeBody(config.data)
        const timeout = config.timeout || 20000

        const args = {
          url,
          method: (config.method || 'get').toUpperCase(),
          headers,
          timeout,
        }
        if (kind === 'text') args.body = body
        if (kind === 'multipart') {
          args.parts = parts
          args.boundary = boundary
        }

        const r = await nativeRequest(args)

        // TOFU / 网络类错误 → 带 tofu 信息的 rejection（api.js 拦截器统一弹确认并重试）
        if (!r.ok) {
          const info = {
            host: r.host || '',
            port: r.port,
            fingerprint: r.fingerprint || '',
            pinnedFingerprint: r.pinnedFingerprint || '',
            message: r.message || '',
            notAfter: r.notAfter || 0,
          }
          reject(makeTofuError(r.code || 'NETWORK_ERROR', info, config))
          return
        }

        // HTTP 响应（含 4xx/5xx，交给 axios validateStatus / 响应拦截器处理）
        let data = r.body == null ? '' : r.body
        const respHeaders = r.headers || {}
        const ctype = String(respHeaders['Content-Type'] || respHeaders['content-type'] || '').toLowerCase()
        if (ctype.includes('application/json') || (data.startsWith('{') && data.endsWith('}'))) {
          try {
            data = JSON.parse(data)
          } catch (e) {
            /* 保留原文 */
          }
        }
        resolve({
          data,
          status: r.status || 0,
          statusText: '',
          headers: respHeaders,
          config,
          request: {},
        })
      } catch (e) {
        // 文件过大等本地错误
        if (e && e.isTofuError) {
          reject(e)
        } else {
          const err = new Error((e && e.message) || '网络连接失败')
          err.isTofuError = true
          err.config = config
          reject(err)
        }
      }
    })()
  })
}
