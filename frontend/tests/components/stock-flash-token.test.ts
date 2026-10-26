/**
 * 涨跌色语法契约(2026-10-23 设计走查 P0, §5.1「行情闪烁实际失效」)。
 *
 * 事故: `--stock-up: #E53935`(Hex) 被塞进 `hsl(var(--stock-up) / .22)` —— 这是**非法 CSS**,
 * 浏览器丢掉整条声明, 于是 sida-flash-up/down 的关键帧没有任何颜色, 涨跌闪静默失效(不报错、
 * 不告警, 只能靠肉眼看)。同类错误一旦再被写回来, 类型检查/构建/lint 全都不会红, 所以必须
 * 由这里兜住:
 *   1. 全文扫 `hsl(var(--X))` / `hsl(var(--X) / a)`, 凡 X 解析到 **Hex/rgb/oklch** 即失败
 *      (含 `--a: var(--b)` 别名链, 防止换个名字再犯);
 *   2. 涨跌 HSL 三元组必须与 Hex **同色**(往返 8bit 误差 ≤ 1), 防止抄错数字;
 *   3. `--stock-up/down` 的 Hex 值不许动(组件/ECharts/--gs-go 都依赖它)。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const ROOT = resolve(__dirname, '../..')
const css = readFileSync(resolve(ROOT, 'src/index.css'), 'utf-8')

/** 去掉注释, 否则注释里的反例(如 `hsl(var(--stock-up) / .22)` 示例)会误判 */
const cssNoComments = css.replace(/\/\*[\s\S]*?\*\//g, '')

/** 所有自定义属性声明: 名 → 值列表(亮/暗/dim 各一处) */
function declarations(source: string): Map<string, string[]> {
  const map = new Map<string, string[]>()
  for (const m of source.matchAll(/(--[\w-]+)\s*:\s*([^;{}]*);/g)) {
    const name = m[1]
    const value = m[2].trim()
    map.set(name, [...(map.get(name) ?? []), value])
  }
  return map
}

const DECLS = declarations(cssNoComments)

/** 变量值的颜色记法; var() 别名继续向上解析(限深防环) */
function colorSyntax(name: string, depth = 0): 'hsl-triplet' | 'hex' | 'rgb' | 'oklch' | 'other' {
  const values = DECLS.get(name)
  if (!values || values.length === 0 || depth > 5) return 'other'
  const kinds = values.map((v) => singleSyntax(v, depth))
  // 同名变量在多个主题块里都定义时, 必须**全部**是可进 hsl() 的三元组
  return kinds.every((k) => k === kinds[0]) ? kinds[0] : 'other'
}

function singleSyntax(value: string, depth: number): 'hsl-triplet' | 'hex' | 'rgb' | 'oklch' | 'other' {
  const v = value.trim()
  const alias = v.match(/^var\(\s*(--[\w-]+)\s*\)$/)
  if (alias) return colorSyntax(alias[1], depth + 1)
  if (/^#[0-9a-f]{3,8}$/i.test(v)) return 'hex'
  if (/^rgba?\(/i.test(v)) return 'rgb'
  if (/^oklch\(/i.test(v)) return 'oklch'
  // HSL 三元组: `240 15% 12%` / `240 15% 12% / a` / 逗号式 `240, 15%, 12%`
  if (/^\d+(?:\.\d+)?(?:deg)?(?:\s*,\s*|\s+)\d+(?:\.\d+)?%(?:\s*,\s*|\s+)\d+(?:\.\d+)?%/.test(v)) return 'hsl-triplet'
  return 'other'
}

/** 全文 hsl(var(--X…) 用法 → [变量名, 行号, 片段] */
function hslVarUsages(source: string): Array<{ name: string; line: number; snippet: string }> {
  const out: Array<{ name: string; line: number; snippet: string }> = []
  const lines = source.split('\n')
  lines.forEach((ln, i) => {
    for (const m of ln.matchAll(/hsl\(\s*var\(\s*(--[\w-]+)/g)) {
      out.push({ name: m[1], line: i + 1, snippet: ln.trim() })
    }
  })
  return out
}

function hslTriplet(name: string): [number, number, number] {
  const m = DECLS.get(name)![0].match(/(\d+(?:\.\d+)?)(?:deg)?\s+(\d+(?:\.\d+)?)%\s+(\d+(?:\.\d+)?)%/)
  expect(m, `${name} 不是 HSL 三元组`).toBeTruthy()
  return [Number(m![1]), Number(m![2]), Number(m![3])]
}

function hexToHsl(hex: string): [number, number, number] {
  const h = hex.replace('#', '')
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255)
  const max = Math.max(r, g, b)
  const min = Math.min(r, g, b)
  const l = (max + min) / 2
  const d = max - min
  if (d === 0) return [0, 0, l * 100]
  const s = d / (1 - Math.abs(2 * l - 1))
  let hue: number
  if (max === r) hue = ((g - b) / d) % 6
  else if (max === g) hue = (b - r) / d + 2
  else hue = (r - g) / d + 4
  hue *= 60
  if (hue < 0) hue += 360
  return [hue, s * 100, l * 100]
}

function hslToRgb8(h: number, s: number, l: number): [number, number, number] {
  const S = s / 100
  const L = l / 100
  const c = (1 - Math.abs(2 * L - 1)) * S
  const hp = (((h % 360) + 360) % 360) / 60
  const x = c * (1 - Math.abs((hp % 2) - 1))
  const [r1, g1, b1] =
    hp < 1 ? [c, x, 0] : hp < 2 ? [x, c, 0] : hp < 3 ? [0, c, x] : hp < 4 ? [0, x, c] : hp < 5 ? [x, 0, c] : [c, 0, x]
  const m = L - c / 2
  return [r1, g1, b1].map((v) => Math.round((v + m) * 255)) as [number, number, number]
}

describe('涨跌色: Hex 事实源 + HSL 三元组派生', () => {
  it('--stock-up / --stock-down 的 Hex 值保持原样(组件/ECharts/--gs-go 依赖)', () => {
    expect(DECLS.get('--stock-up')).toEqual(['#ff5252', '#ff5252'])
    expect(DECLS.get('--stock-down')).toEqual(['#43A047', '#43A047'])
  })

  it('亮/暗两套主题都定义了 --stock-up-hsl / --stock-down-hsl(且都是 HSL 三元组)', () => {
    for (const name of ['--stock-up-hsl', '--stock-down-hsl']) {
      const values = DECLS.get(name)
      expect(values, `${name} 未定义`).toBeTruthy()
      expect(values!.length, `${name} 应亮暗各一处`).toBeGreaterThanOrEqual(2)
      expect(colorSyntax(name), `${name} 必须是 hsl() 可直接吃的三元组`).toBe('hsl-triplet')
    }
  })

  it('新三元组与 Hex 同色(往返 8bit 每通道误差 ≤ 1, 防止抄错数字)', () => {
    for (const [hexVar, hslVar] of [
      ['--stock-up', '--stock-up-hsl'],
      ['--stock-down', '--stock-down-hsl'],
    ] as const) {
      const hex = DECLS.get(hexVar)![0]
      const [eh, es, el] = hexToHsl(hex)
      const [h, s, l] = hslTriplet(hslVar)
      const back = hslToRgb8(h, s, l)
      const orig = [0, 2, 4].map((i) => parseInt(hex.slice(1 + i, 3 + i), 16))
      back.forEach((v, i) => {
        expect(Math.abs(v - orig[i]), `${hslVar} 与 ${hex} 通道 ${i} 偏差过大`).toBeLessThanOrEqual(1)
      })
      expect(Math.abs(h - eh), `${hslVar} 色相偏差`).toBeLessThan(0.5)
      expect(Math.abs(s - es), `${hslVar} 饱和度偏差`).toBeLessThan(0.5)
      expect(Math.abs(l - el), `${hslVar} 亮度偏差`).toBeLessThan(0.5)
    }
  })
})

describe('行情闪烁关键帧(§5.1 P0)', () => {
  it('全文件不再出现 hsl(var(--stock-up…)) / hsl(var(--stock-down…))', () => {
    const banned = cssNoComments.match(/hsl\(\s*var\(\s*--stock-(?:up|down)\s*[/),]/g) ?? []
    expect(banned, 'Hex 变量不能进 hsl() —— 声明会被整条丢弃').toEqual([])
  })

  it('闪色走 --stock-*-hsl + --flash-alpha, 且强度不再是散落魔法数', () => {
    expect(cssNoComments).toMatch(
      /@keyframes sida-flash-up\s*\{\s*0%\s*\{\s*background-color:\s*hsl\(var\(--stock-up-hsl\)\s*\/\s*var\(--flash-alpha\)\)/,
    )
    expect(cssNoComments).toMatch(
      /@keyframes sida-flash-down\s*\{\s*0%\s*\{\s*background-color:\s*hsl\(var\(--stock-down-hsl\)\s*\/\s*var\(--flash-alpha\)\)/,
    )
    expect(DECLS.get('--flash-alpha'), '--flash-alpha 缺失').toBeTruthy()
    // 两个关键帧都必须在 100% 回到 transparent(否则底色会粘住)
    const kf = cssNoComments.match(/@keyframes sida-flash-up\s*\{[\s\S]*?\n\}/)![0]
    expect(kf).toMatch(/100%\s*\{\s*background-color:\s*transparent;/)
  })
})

describe('同类破损全文扫描: hsl(var(--X)) 里 X 必须是 HSL 三元组', () => {
  it('没有任何 hsl(var(--X…)) 指向 Hex / rgb() / oklch() / 无法解析的变量', () => {
    const usages = hslVarUsages(cssNoComments)
    expect(usages.length, 'index.css 里应仍有 hsl(var(--…)) 用法').toBeGreaterThan(10)
    const broken = usages
      .filter((u) => colorSyntax(u.name) !== 'hsl-triplet')
      .map((u) => `index.css:${u.line} hsl(var(${u.name})…) → ${colorSyntax(u.name)}: ${u.snippet}`)
    expect(broken, '以下 hsl() 声明会被浏览器丢弃').toEqual([])
  })
})
