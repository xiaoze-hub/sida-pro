/**
 * G/S 颜色**验收线**(2026-09-19 用户要求做成可测的)。
 *
 * 规则: **A 股惯例 —— G(机会/买入方向)= 红, S(风险/卖出方向)= 绿**
 * (同花顺原版是 G 绿 S 红, SIDA 按国内惯例做了反转; 见 `stock-colors.ts` 的 `GS_COLOR_KIND`)。
 *
 * 这条测试要能拦住三类事故:
 *   ① 有人在调用点手写三元表达式把颜色写反;
 *   ② 有人把 `index.css` 的 `--gs-go/--gs-stop` token 改成绿/红(值层反转);
 *   ③ 有人只改了一处, 导致同页出现"K线买红、交割单买绿"的自相矛盾。
 *
 * 判据是**色相**(hue): 红系 hue<25 或 >330; 绿系 90<hue<170。只比字符串相等会被"换个同色系值"绕过。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  DIRECTION_COLOR_KIND,
  GS_COLOR_KIND,
  directionColorFor,
  directionColorKind,
  gsColorFor,
  gsColorKind,
} from '@panwatch/biz-ui'

// ── 色相工具(把 #rgb / #rrggbb / rgb() / hsl() 统一成 hue) ──────────────────
function toRgb(color: string): [number, number, number] | null {
  const hex = color.trim().match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i)
  if (hex) {
    const h = hex[1].length === 3 ? hex[1].split('').map((c) => c + c).join('') : hex[1]
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)]
  }
  const rgb = color.match(/rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/i)
  if (rgb) return [Number(rgb[1]), Number(rgb[2]), Number(rgb[3])]
  return null
}

function hueOf(color: string): number | null {
  const rgb = toRgb(color)
  if (!rgb) return null
  const [r, g, b] = rgb.map((v) => v / 255)
  const max = Math.max(r, g, b)
  const min = Math.min(r, g, b)
  const d = max - min
  if (d === 0) return 0
  let h = 0
  if (max === r) h = ((g - b) / d) % 6
  else if (max === g) h = (b - r) / d + 2
  else h = (r - g) / d + 4
  return ((h * 60) + 360) % 360
}

const isRedFamily = (c: string) => {
  const h = hueOf(c)
  return h !== null && (h <= 25 || h >= 330)
}
const isGreenFamily = (c: string) => {
  const h = hueOf(c)
  return h !== null && h > 90 && h < 170
}

describe('G/S 颜色规则(单一来源)', () => {
  it('G = 红系(up), S = 绿系(down)', () => {
    expect(GS_COLOR_KIND.G).toBe('up')
    expect(GS_COLOR_KIND.S).toBe('down')
    expect(gsColorKind('G')).toBe('up')
    expect(gsColorKind('S')).toBe('down')
  })

  it('交割单买卖方向同惯例: buy=红, sell=绿(不许与 K 线自相矛盾)', () => {
    expect(DIRECTION_COLOR_KIND.buy).toBe('up')
    expect(DIRECTION_COLOR_KIND.sell).toBe('down')
    expect(directionColorKind('buy')).toBe('up')
    expect(directionColorKind('sell')).toBe('down')
  })

  it('gsColorKind 与 directionColorKind 同源(G/S 不会被单独改反)', () => {
    expect(gsColorKind('G')).toBe(directionColorKind('G'))
    expect(gsColorKind('S')).toBe(directionColorKind('S'))
  })

  it('取色: G 拿 go、S 拿 stop, 两者不得相同', () => {
    const c = { go: '#E53935', stop: '#43A047' }
    expect(gsColorFor('G', c)).toBe('#E53935')
    expect(gsColorFor('S', c)).toBe('#43A047')
    expect(directionColorFor('buy', c)).toBe('#E53935')
    expect(directionColorFor('sell', c)).toBe('#43A047')
    expect(c.go).not.toBe(c.stop)
  })

  it('未知方向兜底为 down(绿), 不默认成"买"', () => {
    // @ts-expect-error 故意传非法值
    expect(directionColorKind('nonsense')).toBe('down')
  })
})

describe('颜色 token: 值层也不能反', () => {
  const css = readFileSync(resolve(__dirname, '../../src/index.css'), 'utf-8')

  /** 取某 token 第一次出现的值(取 := 直接定义的那些) */
  function tokenValue(name: string): string {
    const m = css.match(new RegExp(`${name}\\s*:\\s*([^;]+);`))
    return (m?.[1] || '').trim()
  }

  it('--stock-up 是红系, --stock-down 是绿系(涨红跌绿的总口径)', () => {
    expect(isRedFamily(tokenValue('--stock-up')), tokenValue('--stock-up')).toBe(true)
    expect(isGreenFamily(tokenValue('--stock-down')), tokenValue('--stock-down')).toBe(true)
  })

  it('--gs-go 指向 stock-up(红), --gs-stop 指向 stock-down(绿) —— 不许对调', () => {
    const go = tokenValue('--gs-go')
    const stop = tokenValue('--gs-stop')
    expect(go).toContain('--stock-up')
    expect(stop).toContain('--stock-down')
    expect(go).not.toContain('--stock-down')
    expect(stop).not.toContain('--stock-up')
  })

  it('兜底常量也是红/绿(GS 色解析不到时的最后一道)', () => {
    const fallbacks = readFileSync(resolve(__dirname, '../../packages/biz-ui/src/lib/stock-colors.ts'), 'utf-8')
    const go = fallbacks.match(/go:\s*cssVar\('--gs-go',\s*cssVar\('--stock-up',\s*'([^']+)'\)\)/)
    const stop = fallbacks.match(/stop:\s*cssVar\('--gs-stop',\s*cssVar\('--stock-down',\s*'([^']+)'\)\)/)
    expect(go && isRedFamily(go[1]), `go 兜底=${go?.[1]}`).toBe(true)
    expect(stop && isGreenFamily(stop[1]), `stop 兜底=${stop?.[1]}`).toBe(true)
  })
})

describe('生产验收钩子: K 线容器暴露解析后的颜色', () => {
  it('容器带 data-gs-go / data-gs-stop(canvas 画不进 DOM, 只能这样量)', () => {
    const chart = readFileSync(resolve(__dirname, '../../packages/biz-ui/src/components/KlineChart.tsx'), 'utf-8')
    expect(chart).toContain('data-gs-go={gsResolved.go}')
    expect(chart).toContain('data-gs-stop={gsResolved.stop}')
  })

  it('调用点不许再手写 isBuy ? go : stop 这种三元(规则必须走函数)', () => {
    const chart = readFileSync(resolve(__dirname, '../../packages/biz-ui/src/components/KlineChart.tsx'), 'utf-8')
    expect(chart).not.toMatch(/isBuy\s*\?\s*gs\.go\s*:\s*gs\.stop/)
  })
})
