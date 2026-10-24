/**
 * L2-C 竞价池空态钉子（2026-10-24）
 *
 * 组件直接渲染要拖 `@panwatch/api` + toast provider，本次改动是**空态结构**约束，
 * 用源码断言锁住（与 `nightly-batch-wiring.test.ts` 同手法）。
 *
 * 锁三件事：
 * 1. 空态 = 原因 + 何时有数据 + 一个动作按钮（三件套，不再是一行灰字 + 300px 空表）；
 * 2. 8 个列头各带 `title` 说明（Term 在 frontend/src，biz-ui 无法引用 → 按任务书降级用 title）；
 * 3. 有数据时表格照旧渲染，功能不回退。
 */
import { readFileSync } from 'fs'
import { join } from 'path'
import { describe, expect, it } from 'vitest'

const SRC = readFileSync(
  join(__dirname, '../../packages/biz-ui/src/components/AuctionAnomalyTab.tsx'),
  'utf8',
)

describe('空态三件套（L2-C）', () => {
  it('空态块有断言标记，且不再靠表格空行冒充', () => {
    expect(SRC).toContain('data-auction-empty')
    expect(SRC).not.toContain('colSpan={9}')
  })

  it('原因：保留三因分流（源不可用 / 非交易日 / 非竞价时段 / 无异动）', () => {
    const reasons = ['source_unavailable', 'non_trading_day', 'non_auction_session', 'no_anomaly']
    for (const r of reasons) expect(SRC).toContain(`data.reason === '${r}'`)
    expect(SRC).toContain('emptyReason')
  })

  it('何时有数据：写明 09:15 开始 / 09:25 定型', () => {
    expect(SRC).toContain('09:15')
    expect(SRC).toContain('09:25')
  })

  it('动作：给「重新查询」按钮，调用既有 load', () => {
    expect(SRC).toContain('重新查询')
    expect(SRC).toMatch(/void load\(\)[\s\S]{0,160}重新查询/)
  })
})

describe('表头可解释（8 列各带 title）', () => {
  it('8 个语义列都有 title 说明（序号列免）', () => {
    const thead = SRC.slice(SRC.indexOf('<thead'), SRC.indexOf('</thead>'))
    const ths = thead.match(/<th [^>]*>/g) || []
    expect(ths.length).toBe(9) // 含 # 列
    for (const th of ths.slice(1)) expect(th).toContain('title=')
  })
})

describe('功能不回退', () => {
  it('有数据时仍渲染表格与自选按钮', () => {
    expect(SRC).toContain('<table className="w-full text-[12px]">')
    expect(SRC).toContain('addToWatchlist')
  })

  it('加载态骨架仍在', () => {
    expect(SRC).toContain('animate-pulse')
  })
})
