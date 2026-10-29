// @vitest-environment node
/**
 * B3（2026-10-29 独立复审）· 跨日判定不许只拿「HH:mm」自证。
 *
 * 复现形态（真实字段都来自后端，`/quotes/batch` 带 `quote_date`，`/stocks/markets/status`
 * 的 `local_time` 目前只有 `HH:MM`）：
 *   - 昨天 15:00 报价：`quote_date=2026-10-28`、`daily_pnl_period=today`（昨天判的 today）；
 *   - 今天 09:31 服务器市场墙钟 `local_time='09:31'`；
 *   - 旧算法只比 `HH:mm`：09:31 − 09:31 = 0 ⇒ 判 `current` ⇒ 隔日报价冒充实时破位。
 *
 * 现在的口径：
 *  1. 报价**日历日**（`quote_date`，缺则取 `quote_time` 的日期段）与**带日期的服务器市场墙钟**
 *     （`localTime` 里带 `YYYY-MM-DD`，或显式 `localDate`）都已知时，必须相等；
 *     不等 ⇒ `stale/period`（已知是隔日，不是"未知"）。
 *  2. 报价自身的日期与时刻日期不一致 ⇒ `unknown/clock_skew`（内部自相矛盾，证明不了时点）。
 *  3. 时钟没有日期时，frontend **不拿 HH:mm 推断跨日**；改为要求调用方对"这份快照仍被轮询
 *     在本地有效期内刷新过"负责（`verified`）—— 过期就 `unknown`（见 useStocksData 的
 *     本地有效期测试）。这条留在这里做"不许自证"的负向断言。
 */
import { describe, expect, it } from 'vitest'

import { freshnessFor, resolveDefenseState } from '@/lib/position-risk'
import type { MarketSessionInfo } from '@/lib/position-risk'

const base = {
  price: 33,
  defense: 34,
  period: 'today',
}

describe('freshnessFor · 日期层必须真正比对日期', () => {
  it('带日期的服务器市场墙钟 ≠ 报价日期 → stale/period（同 HH:mm 也不行）', () => {
    const market: MarketSessionInfo = { localDate: '2026-10-29', localTime: '09:31', status: 'trading' }
    const v = freshnessFor({
      price: 33,
      period: 'today',
      quoteDate: '2026-10-28',
      quoteTime: '2026-10-28 09:31:00',
      market,
    })
    expect(v.freshness).toBe('stale')
    expect(v.reason).toBe('period')
  })

  it('日期相同 + 时刻相同 → current（控制组：不是把所有跨日都用保守值兜住）', () => {
    const market: MarketSessionInfo = { localDate: '2026-10-29', localTime: '09:31', status: 'trading' }
    const v = freshnessFor({
      price: 33,
      period: 'today',
      quoteDate: '2026-10-29',
      quoteTime: '2026-10-29 09:31:00',
      market,
    })
    expect(v.freshness).toBe('current')
    expect(v.ageMinutes).toBe(0)
  })

  it('localTime 里带完整日期时也能比对（后端哪天把日期补上就直接生效）', () => {
    const market: MarketSessionInfo = { localTime: '2026-10-29 09:31', status: 'trading' }
    const stale = freshnessFor({
      price: 33,
      period: 'today',
      quoteDate: '2026-10-28',
      quoteTime: '2026-10-28 09:31:00',
      market,
    })
    expect(stale.freshness).toBe('stale')
    expect(stale.reason).toBe('period')

    const fresh = freshnessFor({
      price: 33,
      period: 'today',
      quoteDate: '2026-10-29',
      quoteTime: '2026-10-29 09:31:00',
      market,
    })
    expect(fresh.freshness).toBe('current')
  })

  it('报价日期与报价时刻自相矛盾 → unknown/clock_skew（不猜哪一个是真的）', () => {
    const v = freshnessFor({
      price: 33,
      period: 'today',
      quoteDate: '2026-10-29',
      quoteTime: '2026-10-28 09:31:00',
      market: { localTime: '09:31', status: 'trading' },
    })
    expect(v.freshness).toBe('unknown')
    expect(v.reason).toBe('clock_skew')
  })

  it('时钟没日期时不靠 HH:mm 自证跨日：调用方说快照已过期（verified=false）→ unknown', () => {
    const v = freshnessFor({
      price: 33,
      period: 'today',
      quoteDate: '2026-10-28',
      quoteTime: '2026-10-28 09:31:00',
      market: { localTime: '09:31', status: 'trading' },
      verified: false,
    })
    expect(v.freshness).toBe('unknown')
    expect(v.reason).toBe('refresh_failed')
  })
})

describe('resolveDefenseState · 隔日同价不得判破位', () => {
  it('昨天 09:31 的同价报价在今天 09:31（带日期时钟）→ stale，不进 breached', () => {
    const r = resolveDefenseState({
      ...base,
      quoteDate: '2026-10-28',
      quoteTime: '2026-10-28 09:31:00',
      market: { localDate: '2026-10-29', localTime: '09:31', status: 'trading' },
    })
    expect(r.state).toBe('stale')
    expect(r.freshness).toBe('stale')
    expect(r.reason).toBe('period')
    expect(r.distancePct).toBeCloseTo(-2.941, 2)
  })

  it('行情行里的 quote_date 会一路传到判定（rowFromPosition → buildPositionRiskReport）', async () => {
    const { buildPositionRiskReport } = await import('@/lib/position-risk')
    const report = buildPositionRiskReport(
      [
        {
          id: 1,
          account_id: 1,
          symbol: '600111',
          market: 'CN',
          current_price: 33,
          cost_price: 36.52,
          quantity: 100,
          defense_price: 34,
          quote_date: '2026-10-28',
          quote_time: '2026-10-28 09:31:00',
          daily_pnl_period: 'today',
        },
      ],
      {
        marketSessions: {
          CN: { localDate: '2026-10-29', localTime: '09:31', status: 'trading' },
        },
      },
    )
    expect(report.rows[0].defenseState).toBe('stale')
    expect(report.breached).toBe(0)
    expect(report.rows[0].staleReason).toBe('period')
  })
})
