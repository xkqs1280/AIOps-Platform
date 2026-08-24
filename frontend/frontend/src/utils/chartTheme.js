// ECharts 主题色：根据当前 html.dark 状态返回图表配色（canvas 渲染无法用 CSS 变量，需 JS 读取）
export function chartTheme() {
  const dark = document.documentElement.classList.contains('dark')
  return {
    text: dark ? '#e5e7eb' : '#1e293b',            // 主文本 / title（亮色 slate-800，对比更强）
    sub: dark ? '#94a3b8' : '#475569',             // 次要文本 / legend（亮色 slate-600）
    axis: dark ? '#94a3b8' : '#475569',            // 轴标签（亮色 slate-600）
    axisLine: dark ? 'rgba(148,163,184,0.4)' : 'rgba(71,85,105,0.35)',
    split: dark ? 'rgba(148,163,184,0.15)' : 'rgba(71,85,105,0.15)',
    tooltipBg: dark ? 'rgba(17,24,39,0.92)' : 'rgba(255,255,255,0.97)',
    tooltipBorder: dark ? '#374151' : '#cbd5e1',
    tooltipText: dark ? '#e5e7eb' : '#0f172a',
    brand: dark ? '#22d3ee' : '#0891b2',          // 品牌强调色（亮色 brand-600 保证对比度）
    pieBorder: dark ? '#0f172a' : '#ffffff',      // 饼图切片描边（亮色白边，暗色深边）
  }
}

// 监听主题切换（App.vue 切换时派发 'aiops-theme-changed'）
export function onThemeChange(cb) {
  window.addEventListener('aiops-theme-changed', cb)
  return () => window.removeEventListener('aiops-theme-changed', cb)
}
