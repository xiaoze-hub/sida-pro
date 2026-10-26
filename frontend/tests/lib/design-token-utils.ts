import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import postcss from 'postcss'

export type UiTheme = 'light' | 'dark' | 'dim'
const css = postcss.parse(readFileSync(resolve(__dirname, '../../src/index.css'), 'utf8'))

/** 按真实选择器合并主题，避免将首个 Dim 声明误认成其他主题。 */
export function tokens(theme: UiTheme): Record<string, string> {
  const values: Record<string, string> = {}
  const selectors = [':root', ...(theme !== 'light' ? ['.dark'] : []), ...(theme === 'dim' ? [".dark[data-theme='dim']"] : [])]
  for (const selector of selectors) {
    css.walkRules((rule) => {
      if (rule.selector !== selector) return
      if (rule.parent?.type === 'atrule' && rule.parent.name === 'media') return
      rule.walkDecls((decl) => { values[decl.prop] = decl.value })
    })
  }
  return values
}

export function resolved(values: Record<string, string>, name: string, depth = 0): string {
  if (depth > 8 || !values[name]) throw new Error(`Unresolved token: ${name}`)
  const value = values[name]
  const alias = value.match(/^var\((--[\w-]+)\)$/)
  return alias ? resolved(values, alias[1], depth + 1) : value
}

export function luminance(color: string): number {
  let rgb: number[]
  if (color.startsWith('#')) {
    rgb = [1, 3, 5].map((index) => parseInt(color.slice(index, index + 2), 16) / 255)
  } else {
    const hsl = color.match(/^([\d.]+)\s+([\d.]+)%\s+([\d.]+)%$/)
    if (!hsl) throw new Error(`Unsupported color: ${color}`)
    const h = Number(hsl[1]) / 60
    const s = Number(hsl[2]) / 100
    const l = Number(hsl[3]) / 100
    const c = (1 - Math.abs(2 * l - 1)) * s
    const x = c * (1 - Math.abs(h % 2 - 1))
    const m = l - c / 2
    rgb = [[c, x, 0], [x, c, 0], [0, c, x], [0, x, c], [x, 0, c], [c, 0, x]][Math.floor(h) % 6].map((value) => value + m)
  }
  const linear = rgb.map((value) => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4)
  return linear[0] * .2126 + linear[1] * .7152 + linear[2] * .0722
}

export function contrast(a: string, b: string): number {
  const l1 = luminance(a)
  const l2 = luminance(b)
  return (Math.max(l1, l2) + .05) / (Math.min(l1, l2) + .05)
}
