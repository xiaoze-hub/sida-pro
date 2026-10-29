/**
 * 行情时效 / 防线复审 · 契约测试（2026-10-29 独立复审 B1 / B2 / B3 / B5）。
 *
 * 这份文件守的是**架构约束**，不是渲染（渲染行为见
 * `tests/pages/stocks-snapshot-expiry.test.tsx`、`tests/components/defense-banner-lines-refresh.test.tsx`）：
 *
 *  B1 `fetchAPI` 的超时窗口必须覆盖响应体：`_attemptFetch` 里不许在 `fetch()` 返回处
 *     `clearTimeout`（头到了不算完成）。上游 hook 也必须有自己的本地有效期，
 *     不能把"请求发出去了"当"时钟刷新了"。
 *  B2 期间不可证的涨跌幅不许进当日盈亏篮子（只有一个期间白名单）。
 *  B3 跨日判定必须能比对日期；`HH:mm` 不许单独自证同一分钟。
 *  B5 防线持仓行必须能续期 + 收广播，且续期必须**真取**（绕开 30s GET 缓存），
 *     否则"20s 续期"命中缓存 = 空动作。
 */
import { readFileSync } from 'fs'
import { resolve } from 'path'

import { describe, expect, it } from 'vitest'

const read = (rel: string) => readFileSync(resolve(__dirname, '../..', rel), 'utf-8')
const stripComments = (src: string) =>
  src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1')

const client = read('packages/api/src/client.ts')
const stocksData = read('src/pages/stocks/useStocksData.ts')
const banner = read('src/components/DefenseLineBanner.tsx')
const actions = read('src/pages/stocks/useStocksActions.ts')
const shared = read('src/pages/stocks/shared.ts')
const riskLib = read('src/lib/position-risk.ts')
const events = read('src/lib/defense-lines.ts')

describe('B1 · 请求超时覆盖响应体 + 本地有效期', () => {
  it('_attemptFetch 返回句柄（计时器由调用方在读完体后释放），不在 fetch 返回处清表', () => {
    expect(client).toContain('interface FetchAttempt')
    expect(client).toContain('dispose: () => void')
    expect(client).toContain('timedOut: () => boolean')
    // 读体期间被 abort ⇒ 仍归类 TIMEOUT（不是"解析失败"）
    expect(stripComments(client)).toContain('if (handle.timedOut())')
    // 免责形态：`_attemptFetch` 的 finally 里 clearTimeout（旧 bug 的写法）
    expect(client).not.toMatch(/finally\s*\{\s*if \(timeoutId !== null\)/)
  })

  it('`fetchAPI` 读完体才 dispose（`_parseResponse` 在 try 里，finally 才释放计时器）', () => {
    const body = stripComments(client)
    const parseIdx = body.indexOf('return await _parseResponse<T>(handle.res')
    const finallyIdx = body.indexOf('} finally {\n      handle.dispose()', parseIdx)
    expect(parseIdx).toBeGreaterThan(0)
    expect(finallyIdx).toBeGreaterThan(parseIdx)
  })

  it('useStocksData 有独立本地有效期常量与过期降级（不依赖请求能不能回来）', () => {
    expect(stocksData).toContain('export const MARKET_CLOCK_TTL_MS')
    expect(stocksData).toContain('export const QUOTE_SNAPSHOT_TTL_MS')
    expect(stocksData).toContain('expireStaleSnapshots')
    expect(stocksData).toContain('expireQuoteSnapshots')
    // 心跳先过期再刷新（否则挂死的请求会让旧时钟活到天亮）
    const heartbeat = stocksData.slice(stocksData.indexOf('const freshnessHeartbeat'))
    expect(heartbeat.indexOf('expireStaleSnapshots()')).toBeLessThan(
      heartbeat.indexOf('refreshMarketStatus()'),
    )
    // 心跳闭包里不许出现 Date.now() 之外的"请求状态"判断 —— 记账必须是本地时间戳
    expect(stocksData).toContain('quotePolledAtRef.current = Date.now()')
  })

  it('休市判定不把状态塞进 useCallback 依赖（否则每次写回都重挂表 = 自激请求环）', () => {
    expect(stocksData).toContain('marketStatusRef.current')
    const fn = stocksData.slice(stocksData.indexOf('const shouldPollQuotesNow'))
    expect(fn.slice(0, fn.indexOf('const freshnessHeartbeat'))).toContain('}, [])')
  })

  it('只有成功轮询才刷新快照有效期（WS tick 不算）', () => {
    const refresh = stocksData.slice(
      stocksData.indexOf('const refreshQuotes'),
      stocksData.indexOf('useEffect(() => {\n  if (!portfolioRaw) return'),
    )
    expect(refresh).toContain('quotePolledAtRef.current = Date.now()')
    // onQuoteTicks 不许动这个记账
    const ticks = stocksData.slice(stocksData.indexOf('const onQuoteTicks'))
    expect(ticks.slice(0, ticks.indexOf('useQuoteStream(onQuoteTicks)'))).not.toContain(
      'quotePolledAtRef',
    )
  })
})

describe('B2 · 期间不可证的涨跌幅不进当日篮子', () => {
  it('只有一个期间白名单（today / previous_trading_day）', () => {
    expect(shared).toContain('TRUSTED_DAILY_PERIODS')
    const defs = shared.match(/const TRUSTED_DAILY_PERIODS/g) ?? []
    expect(defs.length).toBe(1)
    expect(shared).toContain("new Set(['today', 'previous_trading_day'])")
  })

  it('合并层先看期间可不可证，再决定要不要涨跌幅', () => {
    expect(shared).toContain('const periodTrusted = TRUSTED_DAILY_PERIODS.has(daily_pnl_period)')
    expect(shared).toMatch(/const change_pct = priced && periodTrusted/)
  })

  it('摘要层丢掉 unknown 观测（不许把它讲成「最近交易日」）', () => {
    expect(shared).toContain('observations.filter(item => TRUSTED_DAILY_PERIODS.has(item.period))')
  })
})

describe('B3 · 跨日必须能比对日期', () => {
  it('时刻层比对前先比对日期，且要求日期自洽', () => {
    expect(riskLib).toContain('export function wallClockDate')
    expect(riskLib).toContain('quoteDate')
    expect(riskLib).toContain('marketDate')
    expect(riskLib).toContain("verdict('stale', 'period')")
  })

  it('行情行的 quote_date 一路传到判定（rowFromPosition 不许丢掉它）', () => {
    expect(riskLib).toMatch(/quoteDate:\s*p\.quote_date/)
  })

  it('useStocksData 认识市场墙钟回绕（跨日信号），并当场作废旧快照', () => {
    expect(stocksData).toContain('hasMarketDayRollover')
    expect(stocksData).toContain('expireQuoteSnapshots(prev)')
  })

  it('applyQuoteTicks 不拿旧日期给"新的涨跌幅"作证（同价也要比涨跌幅）', () => {
    const ticks = read('src/pages/stocks/quote-ticks.ts')
    expect(ticks).toContain('sameChangePct')
  })
})

describe('B5 · 防线持仓行必须能续期且收到别处的保存', () => {
  it('告警条订阅模块级 + window 两级广播，并在心跳里续期持仓行', () => {
    expect(banner).toContain('onDefenseLinesChanged(reload)')
    expect(banner).toContain('onWindowDefenseLinesChanged(reload)')
    expect(events).toContain('DEFENSE_LINES_CHANGED_EVENT')
    const tick = banner.slice(banner.indexOf('const refreshTick'))
    expect(tick.slice(0, tick.indexOf('usePausedInterval(refreshTick'))).toContain('loadPositions()')
  })

  it('续期必须绕开 30s GET 缓存（否则"续期"是空动作）', () => {
    expect(banner).toContain("'/portfolio/summary?include_quotes=false', {\n        cacheMode: 'reload',")
    expect(banner).toContain("'/stocks/markets/status', { cacheMode: 'reload' })")
    expect(banner).toMatch(/\/quotes\/\$\{encodeURIComponent\(symbol\)\}\?market=\$\{encodeURIComponent\(market\)\}`,\s*\{ cacheMode: 'reload' \}/)
  })

  it('保存/删除防线与持仓都会广播', () => {
    expect(banner).toContain('notifyDefenseLinesChanged()')
    expect(actions).toContain("import { notifyDefenseLinesChanged } from '@/lib/defense-lines'")
    const del = actions.slice(actions.indexOf('const handleDeletePosition'))
    expect(del.slice(0, del.indexOf('const toggleAgent'))).toContain('notifyDefenseLinesChanged()')
    const submit = actions.slice(actions.indexOf('const handlePositionSubmit'))
    expect(submit.slice(0, submit.indexOf('const handleDeletePosition'))).toContain(
      'notifyDefenseLinesChanged()',
    )
  })

  it('持仓行续期失败不许继续拿旧防线报警（verified 闸门进判定）', () => {
    expect(banner).toContain('quote.verified && position.verified')
    expect(banner).toContain('不在持仓中')
  })

  it('文案不越线：不写买卖/加减仓/目标价', () => {
    for (const src of [banner, events]) {
      expect(stripComments(src)).not.toMatch(/目标价|建议买入|建议卖出|加仓|减仓/)
    }
  })
})
