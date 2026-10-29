/**
 * 持仓风险检查 · 契约测试（2026-10-29, P2 持仓风险切片；2026-10-29 二/三次评审补充）。
 *
 * 这份文件守的是**架构约束**，不是渲染：
 *  1. 破位/逼近的判定只有一份实现（`@/lib/position-risk`）。`DefenseLineBanner` 曾自己写
 *     `price <= defense` + `distance <= 3`；两处并存时任何一边调阈值都会出现
 *     「告警条没响、体检清单说破位」，故用源码断言钉死单源。
 *  2. 持仓页的检查清单**不新建数据源**：它只吃页面已加载的 `/portfolio/summary` 与
 *     `/stocks/markets/status`（页面上下文），源文件里不许出现 `fetchAPI` / 端点字符串。
 *  3. 文案不越线：不写买卖、加减仓、目标价。
 *  4. **多账户不许含糊聚合**：告警条不许 `rows.find(symbol)` 取首个账户的防线；
 *     检查清单每行必须带账户归属（`account_id`）。
 *  5. **不许自推交易日历 / 自读系统时钟**：新鲜度分层用后端 `daily_pnl_period`（日期层）
 *     + `/stocks/markets/status` 的服务器市场墙钟（时刻层）。库内不出现
 *     `Date.now()` / `new Date()`，也不出现交易日历推断。
 *  6. **时刻层阈值必须显式标注为推断值**：`QUOTE_FRESH_WINDOW_MINUTES` 只允许在 lib 里
 *     定义一次，UI 必须把它写进说明文字；不许出现「5 秒内算新鲜」这种未经证明的假标签。
 *  7. **取价必须带市场**：告警条打 `/quotes/{symbol}` 必须带 `market`，否则后端按 CN 解析，
 *     港美股永远缺价。成本/预设必须按账户行自己的 `cost_price`，不许共用父组件那一份。
 *
 * 2026-10-29 三次评审新增（缺行情口径收紧）：
 *  8. 总资产在部分覆盖时必须是 null（`total_assets_complete=false` + 子集口径），
 *     不许把「已定价市值 + 资金」当总资产展示；
 *  9. 当日盈亏必须带 `daily_pnl_coverage`，一只都没观测到时合计为 null；
 * 10. JSX 文案里不许出现 markdown 星号（会原样渲染）。
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
const shared = read('src/pages/stocks/shared.ts')
const summarySection = read('src/pages/stocks/PortfolioSummarySection.tsx')
const accountsSection = read('src/pages/stocks/AccountsSection.tsx')
const stocksData = read('src/pages/stocks/useStocksData.ts')

describe('防线判定单源', () => {
  it('DefenseLineBanner 使用共享判定，不再自写阈值', () => {
    expect(banner).toMatch(/from '@\/lib\/position-risk'/)
    expect(banner).toContain('resolveDefenseState(')
    expect(banner).not.toMatch(/const NEAR_LINE_PCT = 3/)
    expect(banner).not.toMatch(/price <= defense/)
    expect(banner).not.toMatch(/distancePct <= NEAR_LINE_PCT/)
    expect(banner).not.toMatch(/NEAR_LINE_PCT = /)
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

  it('新鲜度分层：日期层认后端 period，时刻层认服务器市场墙钟，不自读系统时钟', () => {
    expect(lib).not.toMatch(/Date\.now\(\)/)
    expect(lib).not.toMatch(/new Date\(\)/)
    expect(lib).not.toMatch(/isTradingDay|tradingCalendar|isTradingDate|getDay\(\)/)
    expect(lib).toContain('daily_pnl_period')
    // 时刻层必须来自服务器市场状态（local_time / status），不是本机时间
    expect(lib).toContain('marketSessions')
    expect(lib).toContain('localTime')
    expect(lib).toContain('QUOTE_FRESH_WINDOW_MINUTES')
  })

  it('时效窗口是「推断值」：lib 里只定义一次，UI 必须写明它是推断值', () => {
    expect(lib.match(/export const QUOTE_FRESH_WINDOW_MINUTES = 15/g)?.length).toBe(1)
    expect(lib).toContain('推断值')
    expect(section).toContain('QUOTE_FRESH_WINDOW_MINUTES')
    expect(section).toContain('推断值')
    // 未经证明的「5 秒新鲜」标签不许出现
    expect(stripComments(section)).not.toMatch(/5\s*秒/)
    expect(stripComments(banner)).not.toMatch(/5\s*秒/)
  })

  it('检查清单把市场状态传进判定（不自己判交易日历）', () => {
    expect(section).toContain('marketStatus')
    expect(section).toContain('marketSessions')
    expect(section).toMatch(/buildPositionRiskReport\([^)]*marketSessions/s)
  })
})

describe('多账户 / 多市场', () => {
  it('告警条不取首个账户的防线（旧 bug：rows.find(symbol)）', () => {
    expect(banner).not.toMatch(/rows\.find\(\s*\(p\)\s*=>\s*p\.symbol/)
    expect(banner).not.toMatch(/\.find\(\(p\) => p\.symbol === symbol\)/)
    // 必须按账户逐个取，且能显示账户名
    expect(banner).toMatch(/defense-line-account-/)
    expect(banner).toContain('account')
  })

  it('告警条取价必须带 market，成本/预设按账户行', () => {
    expect(banner).toContain('market=${encodeURIComponent(market)}')
    expect(banner).toMatch(/costPrice/)
    expect(banner).toMatch(/cost_price/)
    // 不许把父组件传进来的成本直接当每个账户的成本
    expect(stripComments(banner)).not.toMatch(/const costVal = cost as number/)
  })

  it('检查清单每行带账户归属，不做跨账户防线聚合', () => {
    expect(section).toContain('account_id')
    expect(section).toContain('account_name')
    expect(lib).toContain('conflictingSymbols')
    // 旧 bug 的形态：把后一账户的防线覆盖到前一行（`prev.defense = defense`）。
    expect(stripComments(lib)).not.toMatch(/prev\.defense\s*=\s*defense/)
  })

  it('WS 行情推送不许抹掉报价时刻（拿不到新时刻 ≠ 时刻不存在），也不许自造时刻', () => {
    const code = stripComments(stocksData)
    // 旧形态：quote_time: null / quote_date: null 直接写死
    expect(code).not.toMatch(/quote_time:\s*null/)
    expect(code).not.toMatch(/quote_date:\s*null/)
    expect(code).toMatch(/quote_time:\s*prevQuote\?\.quote_time/)
    // 不许拿本机时钟冒充报价时刻
    expect(code).not.toMatch(/quote_time:\s*new Date/)
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

describe('缺行情 / 覆盖度口径（三次评审）', () => {
  it('总资产部分覆盖时必须是 null，另给「已定价子集」', () => {
    expect(shared).toContain('total_assets_priced_subset')
    expect(shared).toContain('total_assets_complete')
    expect(shared).toContain("priced_positions_only")
    // 旧的写法：只要有一只定价就算 total_assets
    expect(stripComments(shared)).not.toMatch(/grandTotalAssets = grandMarketValue \+ grandAvailable/)
    expect(summarySection).toContain('total_assets_priced_subset')
  })

  it('当日盈亏有覆盖度，缺涨跌幅不静默少算', () => {
    expect(shared).toContain('daily_pnl_coverage')
    expect(shared).toContain('positions_observed')
    expect(accountsSection).toContain('daily_pnl_coverage')
    expect(summarySection).toContain('daily_pnl_coverage')
  })

  it('账户行不冒称「只」：持仓条数按「条」表述', () => {
    expect(accountsSection).toContain('条持仓')
    expect(accountsSection).not.toMatch(/\{account\.positions\.length\} 只/)
  })

  it('JSX 文案里不出现 markdown 星号', () => {
    const jsxText = (src: string) => stripComments(src)
    for (const src of [section, banner, summarySection, accountsSection]) {
      expect(jsxText(src)).not.toContain('**')
    }
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
    expect(section).toContain('以下方持仓表为准')
  })
})
