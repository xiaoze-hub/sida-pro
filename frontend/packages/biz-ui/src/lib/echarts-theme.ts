/**
 * v0.4.8 → v0.5.0: 「终端感」ECharts 主题 — 对齐 SIDA 设计 token。
 * 双态: 从 CSS 变量读取颜色值, 自动适配 light/dark 模式。
 * 用法: echarts.registerTheme('sida', buildSidaTheme()) 后 init(dom, 'sida')。
 * 色值与 index.css 的 --echarts-* 语义 token 保持一致。
 */

/** 从 CSS 变量读取颜色值, 回退到 hardcoded 默认值(用于 SSR / 报错兜底)。 */
function cssVar(name: string, fallback: string): string {
  if (typeof document === 'undefined') return fallback
  try {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
  } catch {
    return fallback
  }
}

export function buildSidaTheme() {
  return {
    /* 图表画布(2026-09-20): 用 --chart-canvas(比外壳更空一档), 不再交给默认值 ——
       生产实测过一块**白色画布**(深色主题里热力图画布底色是白的), 显式给值才不会再漂。 */
    backgroundColor: `hsl(${cssVar('--chart-canvas', '240 20% 3.5%')})`,
    textStyle: { color: cssVar('--echarts-text', '#c4c4cb') },
    axisPointer: {
      lineStyle: { color: cssVar('--echarts-crosshair', 'rgba(120,120,130,.4)') },
      crossStyle: { color: cssVar('--echarts-crosshair', 'rgba(120,120,130,.4)') },
    },
    categoryAxis: {
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: cssVar('--echarts-axis-label', '#8e8e96'), fontSize: 10 },
      splitLine: { show: false },
    },
    valueAxis: {
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        color: cssVar('--echarts-axis-label', '#8e8e96'),
        fontSize: 10,
        fontFamily: 'monospace',
      },
      splitLine: {
        lineStyle: { color: cssVar('--echarts-split-line', 'rgba(120,120,130,.12)') },
      },
    },
    tooltip: {
      backgroundColor: cssVar('--echarts-tooltip-bg', 'rgba(24,24,27,.95)'),
      borderColor: cssVar('--echarts-tooltip-border', 'rgba(120,120,130,.3)'),
      borderWidth: 1,
      textStyle: { color: cssVar('--echarts-tooltip-text', '#fafafa'), fontSize: 11 },
      extraCssText: 'backdrop-filter: blur(4px); border-radius: 6px;',
    },
  }
}

export const SIDA_THEME_NAME = 'sida'