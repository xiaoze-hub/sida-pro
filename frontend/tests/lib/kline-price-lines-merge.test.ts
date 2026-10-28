/**
 * `mergePriceLines` 纯函数回归（2026-10-28 复审阻断 ③）。
 *
 * 图表把**页面接线的已确认标注**（`supportPressure`）与**自己取到的图层价线**
 * （`/klines/{symbol}/summary` 的 `unlock_levels`：筹码套牢/支撑压力）合并成一组。
 * 这里逐个钉住合并语义（不渲染组件，跑得快、边界说得清）：
 *
 *  - primary（服务端标注）优先：同价位同向只画一条，标签用标注的；
 *  - 价位口径 2 位小数（与 `linesForTarget`/`annotationLedger.mergeLines` 一致）；
 *  - 方向不同不算重复（同价一支撑一压力是两条线，图上颜色不同）；
 *  - 非法价/未知方向丢弃（不猜，不喂脏数据给画布）；
 *  - 一侧为空时原样返回另一侧（同引用）—— 页面行为不变、不触发多余重绘。
 */
import { describe, expect, it } from 'vitest'

import { mergePriceLines, type KlinePriceLine } from '@sida/biz-ui/klineEvents'

const line = (
  price: number,
  kind: 'support' | 'pressure',
  label?: string,
  ratio?: number | null,
): KlinePriceLine => ({ price, kind, label, ratio })

describe('mergePriceLines：确认标注 + 图层价线', () => {
  it('同向同价只留一条，标注（primary）的标签优先，按价升序排列', () => {
    const merged = mergePriceLines(
      [line(36.5, 'pressure', '阻力位'), line(32.8, 'support', '支撑位')],
      [line(32.8, 'support', '筹码支撑 32.8'), line(40, 'pressure', '筹码压力 40')],
    )
    expect(merged.map((l) => `${l.kind}@${l.price}:${l.label}`)).toEqual([
      'support@32.8:支撑位',
      'pressure@36.5:阻力位',
      'pressure@40:筹码压力 40',
    ])
  })

  it('价位按 2 位小数比较：32.80 / 32.8 是同一档，32.804 也是', () => {
    const merged = mergePriceLines(
      [line(32.8, 'support', '标注')],
      [line(32.8, 'support', '筹码 a'), line(32.804, 'support', '筹码 b'), line(32.81, 'support', '筹码 c')],
    )
    expect(merged.map((l) => l.label)).toEqual(['标注', '筹码 c'])
  })

  it('同价不同向不合并（图上是一条支撑一条压力）', () => {
    const merged = mergePriceLines([line(50, 'support', '支撑')], [line(50, 'pressure', '压力')])
    expect(merged).toHaveLength(2)
    expect(merged.map((l) => l.kind)).toEqual(['support', 'pressure'])
  })

  it('非法价（<=0 / NaN / Infinity）与未知方向一律丢弃', () => {
    const dirty = [
      line(0, 'support', '零价'),
      line(-1, 'support', '负价'),
      line(Number.NaN, 'support', 'NaN'),
      line(Number.POSITIVE_INFINITY, 'pressure', 'Infinity'),
      { price: 12, kind: 'unknown' as unknown as 'support', label: '未知方向' },
      line(12.5, 'support', '有效'),
    ]
    const merged = mergePriceLines(dirty as KlinePriceLine[], [])
    expect(merged.map((l) => l.label)).toEqual(['有效'])
  })

  it('一侧为空时原样返回另一侧（同引用，不制造无意义的状态变更）', () => {
    const layerLines = [line(32.8, 'support', '筹码')]
    expect(mergePriceLines(undefined, layerLines)).toBe(layerLines)
    expect(mergePriceLines([], layerLines)).toBe(layerLines)

    const annotations = [line(32.8, 'support', '支撑位')]
    expect(mergePriceLines(annotations, undefined)).toBe(annotations)
    expect(mergePriceLines(annotations, [])).toBe(annotations)

    // 都没有 → 稳定空数组（两次调用同一个引用）
    const empty = mergePriceLines(undefined, undefined)
    expect(empty).toEqual([])
    expect(mergePriceLines([], [])).toBe(empty)
  })
})
