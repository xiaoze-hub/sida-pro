import { cardSurfaces, cardBorderClasses } from './ui-card-surfaces.mjs'

/** 可单独测试的 UI 准则；只扫描产品源码，不依赖浏览器或构建器。 */
export function auditPresentation(source) {
  const rules = [
    ['R21-SHARED-SWITCH', /\brole=["']switch["']/g, '开关复用共享 Switch，禁止手写轨道和圆点'],
    ['R17-OPAQUE-SURFACE', /\bbg-(?:card|background|popover|muted|accent|s[0-4])\/(?:\[[^\]]+\]|\d+)/g,
      '空间面板必须使用不透明 token，禁止按页面叠加透明度'],
    ['R18-READABLE-TYPE', /\btext-\[(?:10|11|13)px\]/g,
      '辅助文字从 12px 起，正文 14px，标题 16/20px'],
    ['R19-STRUCTURAL-BORDER', /\bborder-border\/\d+/g,
      '结构边界使用 border-border / border-border-strong，不再降低透明度'],
  ]
  const issues = rules.flatMap(([rule, pattern, message]) =>
    [...source.matchAll(pattern)].map((hit) => ({
      rule, message, hit: hit[0], line: source.slice(0, hit.index).split('\n').length,
    })),
  )
  for (const surface of cardSurfaces(source)) {
    for (const hit of surface.raw.matchAll(cardBorderClasses)) issues.push({
      rule: 'R22-BORDERLESS-CARD', message: '内容卡片禁止描边与装饰 ring，使用底色和留白分层',
      hit: hit[0], line: source.slice(0, surface.start + hit.index).split('\n').length,
    })
  }
  return issues
}

export function auditThemeAliases(css) {
  const errors = []
  for (const [name, target] of [['background', 's0'], ['card', 's1'], ['popover', 's4']]) {
    const values = [...css.matchAll(new RegExp(`--${name}:\\s*([^;]+);`, 'g'))]
    if (!values.length || values.some((hit) => hit[1].trim() !== `var(--${target})`)) {
      errors.push(`--${name} 必须唯一映射 var(--${target})，主题只覆盖空间 token`)
    }
  }
  if (/:root\[data-theme=['"]dim['"]\]/.test(css)) errors.push('Dim 必须限定 .dark，不能覆盖浅色')
  if (/--panel-highlight:/.test(css)) errors.push('卡片禁止内侧高光，不能用阴影模拟描边')
  if (/\.card\b/.test(css) && !/\.card-surface[^{}]*\{[^{}]*border:\s*none\s*!important/.test(css)) errors.push('共享卡片必须统一强制 border: none')
  return errors
}
