/**
 * 1-D 主力战报钉子（2026-10-24）
 *
 * 该页无渲染测试（数据依赖 `/api/war-report/daily`），本次改动是**结构与列语义**约束，
 * 用源码断言锁住（与 `nightly-batch-wiring.test.ts` 同手法）。
 *
 * 锁三件事：
 * 1. 「净额变化 / 采样数」两列已删（勘误 §7.7：后端从未产出，全列 -- / 0）；
 * 2. 首屏有结论层（方向 / 最集中行业 / 最异常个股），且口径写明「仅榜内合计」；
 * 3. 两张榜（吸筹/派发）仍在，功能不回退。
 */
import { readFileSync } from 'fs'
import { join } from 'path'
import { describe, expect, it } from 'vitest'

const SRC = readFileSync(join(__dirname, '../../src/pages/WarReport.tsx'), 'utf8')

describe('死列已删（§7.7）', () => {
  it('表头不再渲染「净额变化」「采样数」', () => {
    expect(SRC).not.toContain("t('warReport.netChange')")
    expect(SRC).not.toContain("t('warReport.samples')")
  })

  it('行内不再读取 net_change_wan / samples 字段', () => {
    expect(SRC).not.toContain('r.net_change_wan')
    expect(SRC).not.toContain('r.samples')
  })

  it('表头列数收敛为 5 列（# / 代码 / 名称 / 主力净流入 / 数据源）', () => {
    const thead = SRC.slice(SRC.indexOf('<thead'), SRC.indexOf('</thead>'))
    expect((thead.match(/<th /g) || []).length).toBe(5)
  })
})

describe('结论层（首屏先给结论）', () => {
  it('三块结论卡存在，且带可断言标记', () => {
    expect(SRC).toContain('data-testid="war-report-conclusion"')
    for (const k of ['directionLabel', 'topIndustry', 'topStock']) expect(SRC).toContain(`t('warReport.${k}')`)
  })

  it('口径写明「仅榜内合计」，不许冒充全市场', () => {
    expect(SRC).toContain("t('warReport.directionHint')")
    const zh = readFileSync(join(__dirname, '../../src/locales/zh-CN.ts'), 'utf8')
    expect(zh).toContain('仅 TOP20 榜内合计')
  })

  it('缺数据时显式「无数据」而不是隐藏整块', () => {
    expect(SRC).toContain("t('warReport.conclusionEmpty')")
    expect((SRC.match(/conclusionEmpty/g) || []).length).toBeGreaterThanOrEqual(3)
  })

  it('结论层排在两张榜之前（首屏顺序）', () => {
    const conclusion = SRC.indexOf('war-report-conclusion')
    const inflow = SRC.indexOf("t('warReport.topInflow')")
    expect(conclusion).toBeGreaterThan(0)
    expect(conclusion).toBeLessThan(inflow)
  })
})

describe('功能不回退', () => {
  it('吸筹 / 派发两张榜仍在', () => {
    expect(SRC).toContain("t('warReport.topInflow')")
    expect(SRC).toContain("t('warReport.topOutflow')")
    expect((SRC.match(/<RowTable /g) || []).length).toBe(2)
  })

  it('行业分布与拆单/对倒区块仍在', () => {
    expect(SRC).toContain("t('warReport.industryTitle')")
    expect(SRC).toContain("t('warReport.splitWashTitle')")
  })
})
