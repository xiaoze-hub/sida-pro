// P1-2(2026-09-30): 持仓成本线画在 K 线上的**图表侧契约**。
//
// 为什么不渲染 KlineChart 来测: 真渲染会创建 lightweight-charts 实例(jsdom 无 canvas), 成本线的
// **取数/接线**已由 `stock-workbench.test.tsx` 的「P1-2 持仓成本线」用例覆盖。这里只钉来源契约 ——
// 防止日后改 KlineChart 时把 `costLines` 的**画线/守卫**悄悄删掉(无持仓不画、非有限数跳过)。
import { readFileSync } from 'fs'
import { resolve } from 'path'

import { describe, expect, it } from 'vitest'

const KC = readFileSync(
  resolve(__dirname, '../../packages/biz-ui/src/components/KlineChart.tsx'),
  'utf-8',
)

describe('KlineChart 持仓成本线契约(P1-2)', () => {
  it('暴露 costLines 可选 prop(price + title; 无持仓/取不到时调用方不传)', () => {
    expect(KC).toMatch(/costLines\?: Array<\{ price: number; title: string \}>/)
  })

  it('按 props.costLines 逐条 createPriceLine(水平实线 + 轴标签显示 title)', () => {
    // 遍历传入的成本线并建价格线(实线, 与支撑压力虚线区分)
    expect(KC).toMatch(/for \(const line of props\.costLines \|\| \[\]\)/)
    expect(KC).toMatch(/series\.createPriceLine\(\{/)
    expect(KC).toMatch(/axisLabelVisible: true/)
    expect(KC).toMatch(/title: line\.title/)
    expect(KC).toMatch(/lineStyle: 0, \/\/ solid/)
  })

  it('价格非有限数 → 跳过(不画坏线; 禁猜/禁默认值)', () => {
    expect(KC).toMatch(/if \(!Number\.isFinite\(line\.price\)\) continue/)
  })

  it('重绘依赖含 costLines(切股/持仓变化时成本线跟着刷新, 不残留)', () => {
    expect(KC).toMatch(/props\.costLines,/)
    // 重绘前先移除上一轮成本线(否则切股残留旧成本线)
    expect(KC).toMatch(/for \(const line of costLinesRef\.current\) series\.removePriceLine\(line\)/)
  })
})
