/**
 * L2-E 机会页钉子（2026-10-24）
 *
 * 该页 2300 行、无页面级渲染测试（历史无回归网），但本次改动全是**结构与语义**约束，
 * 用源码断言即可锁住 —— 与仓库既有 `nightly-batch-wiring.test.ts` 同一手法。
 *
 * 锁四件事：
 * 1. 首屏 KPI 从 4 张降到 3 张（`md:grid-cols-3`），市场池占比降级进「更多指标」；
 * 2. 止损不再用涨跌色（合法=中性、计划无效=琥珀），两张表格（桌面/移动）同步；
 * 3. 工具页签 7 → 4 + 「更多工具」下拉，且 7 个 TabsContent 一个都不能少；
 * 4. 禁止把 KPI 再塞回首屏（防止后续 lane 顺手加回来）。
 */
import { readFileSync } from 'fs'
import { join } from 'path'
import { describe, expect, it } from 'vitest'

const SRC = readFileSync(join(__dirname, '../../src/pages/Opportunities.tsx'), 'utf8')

describe('机会页首屏结构（L2-E）', () => {
  it('首屏 KPI 容器是 3 列，不再是 4 列', () => {
    expect(SRC).toContain('md:grid-cols-3 gap-3 mb-4')
    expect(SRC).not.toContain('md:grid-cols-4 gap-3 mb-4')
  })

  it('市场池占比移入「更多指标」，默认折叠', () => {
    expect(SRC).toContain('更多指标（市场池占比 · 因子与约束 5 项）')
    const detailsIdx = SRC.indexOf('更多指标（市场池占比')
    const marketShareIdx = SRC.indexOf("t('opportunities.marketShare')")
    expect(marketShareIdx).toBeGreaterThan(detailsIdx)
  })

  it('「更多指标」用原生 details 实现，不新增组件依赖', () => {
    expect(SRC).toMatch(/<details className="mb-4 border-y border-border py-2">/)
    expect(SRC).toMatch(/<summary[^>]*>[\s\S]{0,80}更多指标/)
  })
})

describe('止损/目标配色（G12 同含义一种颜色）', () => {
  it('止损合法态用中性灰，不再用跌绿', () => {
    expect(SRC).toContain("planValid ? 'text-muted-foreground' : 'text-amber-500 font-semibold'")
    expect(SRC).not.toContain("planValid ? 'text-stock-down' : 'text-red-500 font-semibold'")
  })

  it('移动端展开区同步：止损中性、目标涨红', () => {
    expect(SRC).toContain('<span className="text-muted-foreground">止损 {formatPlanPrice(stopLoss)}</span>')
    expect(SRC).toContain('<span className="text-stock-up">目标 {formatPlanPrice(targetPrice)}</span>')
  })
})

describe('工具页签 4 + 更多（不删功能）', () => {
  it('首排只保留 4 个 TabsTrigger', () => {
    const list = SRC.slice(SRC.indexOf('<TabsList'), SRC.indexOf('</TabsList>'))
    expect((list.match(/<TabsTrigger/g) || []).length).toBe(4)
    for (const v of ['resonance', 'stockpool', 'jbp', 'strategy']) expect(list).toContain(`value="${v}"`)
  })

  it('后 3 个工具进「更多工具」下拉', () => {
    expect(SRC).toContain('更多工具 ▾')
    for (const v of ['tdx', 'wencai', 'cb']) expect(SRC).toContain(`['${v}',`)
  })

  it('7 个 TabsContent 一个都不能少（功能不回退）', () => {
    for (const v of ['resonance', 'stockpool', 'jbp', 'strategy', 'tdx', 'wencai', 'cb']) {
      expect(SRC).toContain(`<TabsContent value="${v}">`)
    }
  })
})
