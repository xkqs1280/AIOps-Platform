// 明暗主题工具：localStorage `aiops_theme` 存 `system | light | dark`，默认 system（跟随系统）。
// 供 App.vue（侧边栏快捷切换）与显示设置页（三态选择）共用。
const THEME_KEY = 'aiops_theme'

export function getThemeMode() {
  return localStorage.getItem(THEME_KEY) || 'system'
}

export function setThemeMode(mode) {
  localStorage.setItem(THEME_KEY, mode)
}

// 当前模式是否暗色：显式 dark → 暗；显式 light → 亮；system → 跟随操作系统偏好
export function isDarkNow(mode = getThemeMode()) {
  if (mode === 'dark') return true
  if (mode === 'light') return false
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

// 将 .dark class 应用到 <html>，返回是否暗色
export function applyTheme(mode = getThemeMode()) {
  const dark = isDarkNow(mode)
  document.documentElement.classList.toggle('dark', dark)
  return dark
}

// 切换到指定模式：持久化 → 应用 → 派发事件 → 刷新页面重建 ECharts 图表（canvas 无法用 CSS 变量实时换肤）
export function switchTheme(mode) {
  setThemeMode(mode)
  applyTheme(mode)
  window.dispatchEvent(new CustomEvent('aiops-theme-changed'))
  setTimeout(() => window.location.reload(), 120)
}
