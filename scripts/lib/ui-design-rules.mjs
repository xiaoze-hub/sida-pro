/** 可单独测试的 UI 准则；只扫描产品源码，不依赖浏览器或构建器。 */
export function auditPresentation(source) {
  const rules = [
    ['R17-OPAQUE-SURFACE', /\bbg-(?:card|background|popover|muted|accent|s[0-4])\/(?:\[[^\]]+\]|\d+)/g,
      '空间面板必须使用不透明 token，禁止按页面叠加透明度'],
    ['R18-READABLE-TYPE', /\btext-\[(?:10|11|13)px\]/g,
      '辅助文字从 12px 起，正文 14px，标题 16/20px'],
    ['R19-STRUCTURAL-BORDER', /\bborder-border\/\d+/g,
      '结构边界使用 border-border / border-border-strong，不再降低透明度'],
  ]
  return rules.flatMap(([rule, pattern, message]) =>
    [...source.matchAll(pattern)].map((hit) => ({
      rule, message, hit: hit[0], line: source.slice(0, hit.index).split('\n').length,
    })),
  )
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
  return errors
}
