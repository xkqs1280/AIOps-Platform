// 移动端 ECharts 主题色：跟随系统明暗（CSS 无法作用于 canvas，需 JS 读取）
export function chartTheme() {
  const dark = window.matchMedia('(prefers-color-scheme: dark)').matches
  return {
    label: dark ? '#e2e8f0' : '#1e293b',   // 节点标签
    line: dark ? '#334155' : '#cbd5e1',    // 连线
    sub: dark ? '#94a3b8' : '#475569',     // 次要文本
  }
}
