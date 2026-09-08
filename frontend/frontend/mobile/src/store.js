// 移动端连接管理：服务器地址 + token 的本地持久化
const IP_KEY = 'aiops_mobile_server'
const TOKEN_KEY = 'aiops_mobile_token'

export function getServer() {
  try {
    return JSON.parse(localStorage.getItem(IP_KEY) || 'null')
  } catch {
    return null
  }
}

// server: { ip, port, remembered }
export function setServer(server) {
  // 切到不同服务器时, 旧 token 必然失效 (跨平台 JWT 不可用), 主动清掉避免短暂 401
  const prev = getServer()
  const sameServer = prev && prev.ip === server.ip && (prev.port || 0) === (server.port || 0)
  if (!sameServer) {
    setToken('')
  }
  if (server.remembered) {
    localStorage.setItem(IP_KEY, JSON.stringify(server))
  } else {
    localStorage.setItem(IP_KEY, JSON.stringify({ ...server, remembered: false }))
  }
}

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

// 根据服务器地址构造 API baseURL：https://ip:port/api/v1（自签名 HTTPS）
export function buildBaseUrl(server) {
  if (!server || !server.ip) return ''
  const port = server.port ? `:${server.port}` : ''
  return `https://${server.ip}${port}/api/v1`
}

export function currentBaseUrl() {
  return buildBaseUrl(getServer())
}
