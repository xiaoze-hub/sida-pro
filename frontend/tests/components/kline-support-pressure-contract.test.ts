import { readFileSync } from 'fs'
import { resolve } from 'path'

import { describe, expect, it } from 'vitest'

const KC = readFileSync(
  resolve(__dirname, '../../packages/biz-ui/src/components/KlineChart.tsx'),
  'utf-8',
)

describe('KlineChart 支撑压力价位线画线契约', () => {
  it('暴露 supportPressure 可选 prop (KlinePriceLine[])', () => {
    expect(KC).toMatch(/supportPressure\?: KlinePriceLine\[\]/)
  })

  // 2026-10-28 复审阻断 ③：`props.supportPressure` 不再"独占"价位线 —— 传了它（哪怕空数组）
  // 图表也要把自己从 summary 取到的 unlock_levels 合并进来，否则参谋标注一上图，
  // 筹码支撑压力整批消失、同价位画两条重合虚线。语义见 `mergePriceLines`。
  it('父传 supportPressure 与自取图层 unlock_levels **合并**（不是父接管覆盖）', () => {
    expect(KC).toMatch(
      /const effPriceLines = useMemo\(\s*\(\) => mergePriceLines\(props\.supportPressure, layer\?\.priceLines\)/,
    )
    // 合并函数本体：同向同价去重（primary 优先）
    const EVENTS = readFileSync(
      resolve(__dirname, '../../packages/biz-ui/src/klineEvents.ts'),
      'utf-8',
    )
    expect(EVENTS).toMatch(/export function mergePriceLines\(/)
    expect(EVENTS).toMatch(/export function normalizePriceLines\(/)
  })

  it('按 effPriceLines 逐条调用 series.createPriceLine 在图表上绘制支撑与阻力线', () => {
    expect(KC).toMatch(/for \(const line of effPriceLines\)/)
    expect(KC).toMatch(/priceLinesRef\.current\.push\(\s*series\.createPriceLine\(\{/)
    expect(KC).toMatch(/price: line\.price/)
    expect(KC).toMatch(/lineStyle: 2, \/\/ dashed/)
  })

  it('重绘前先清空上一轮价位线，避免虚线累积残留', () => {
    expect(KC).toMatch(/for \(const line of priceLinesRef\.current\) series\.removePriceLine\(line\)/)
  })
})
