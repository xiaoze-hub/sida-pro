/**
 * 持仓风险检查 · 契约测试（2026-10-29, P2 持仓风险切片）。
 *
 * 这份文件守的是**架构约束**，不是渲染：
 *  1. 破位/逼近的判定只有一份实现（`@/lib/position-risk`）。`DefenseLineBanner` 曾自己写
 *     `price <= defense` + `distance <= 3`；两处并存时任何一边调阈值都会出现
 *     「告警条没响、体检清单说破位」，故用源码断言钉死单源。
 *  2. 持仓页的检查清单**不新建数据源**：它只吃页面已加载的 `/portfolio/summary`，
 *     源文件里不许出现 `fetchAPI` / 端点字符串。
 *  3. 文案不越线：不写买卖、加减仓、目标价。
 */
import { readFileSync } from 'fs'
import { resolve } from 'path'

import { describe, expect, it } from 'vitest'

const read = (rel: string) => readFileSync(resolve(__dirname, '../..', rel), 'utf-8')

/** 去掉注释再查：注释里解释"为什么不用 /portfolio/xxx"不算新建数据源，也不算文案。 */
const stripComments = (src: string) =>
  src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1')

const banner = read('src/components/DefenseLineBanner.tsx')
const section = read('src/pages/stocks/PositionRiskSection.tsx')
const stocksPage = read('src/pages/Stocks.tsx')
const lib = read('src/lib/position-risk.ts')

describe('防线判定单源', () => {
  it('DefenseLineBanner 使用共享判定，不再自写阈值', () => {
    expect(banner).toMatch(/import \{ evaluateDefenseLine \} from '@\/lib\/position-risk'/)
    expect(banner).toContain('evaluateDefenseLine(price, defense)')
    expect(banner).not.toMatch(/const NEAR_LINE_PCT = 3/)
    expect(banner).not.toMatch(/price <= defense/)
    expect(banner).not.toMatch(/distancePct <= NEAR_LINE_PCT/)
  })

  it('阈值只定义在 lib 一处，并导出给 UI 显示用', () => {
    expect(lib.match(/export const NEAR_LINE_PCT = 3/g)?.length).toBe(1)
    expect(section).toContain('NEAR_LINE_PCT')
    expect(section).not.toMatch(/const NEAR_LINE_PCT = /)
  })

  it('判定函数是纯函数：不 import 网络层 / React', () => {
    expect(lib).not.toMatch(/from '@sida\/api'/)
    expect(lib).not.toMatch(/from 'react'/)
    expect(lib).not.toMatch(/fetchAPI/)
  })
})

describe('不新建数据源', () => {
  it('持仓页已挂载检查清单', () => {
    expect(stocksPage).toContain("import { PositionRiskSection } from './stocks/PositionRiskSection'")
    expect(stocksPage).toContain('<PositionRiskSection />')
  })

  it('检查清单只读页面上下文，自己不发请求', () => {
    const code = stripComments(section)
    expect(section).toContain("import { useStocks } from './context'")
    expect(code).not.toContain('fetchAPI')
    expect(code).not.toMatch(/\/portfolio\//)
    expect(code).not.toContain('/accounts/')
  })
})

describe('文案合规', () => {
  const FORBIDDEN = [/买入/, /卖出/, /加仓/, /减仓/, /抄底/, /目标价/, /稳赚/, /必涨/]

  it('不写买卖指令或收益承诺', () => {
    const copy = stripComments(section)
    for (const re of FORBIDDEN) expect(copy).not.toMatch(re)
  })

  it('保留只读尾注与缺失说明', () => {
    expect(section).toContain('不构成投资建议')
    expect(section).toContain('不参与')
    expect(section).toContain('未设防线不告警')
  })
})
