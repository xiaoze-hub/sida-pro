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

  it('优先消费 props.supportPressure 作为有效价位线', () => {
    expect(KC).toMatch(/const effPriceLines = props\.supportPressure \?\?/)
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
