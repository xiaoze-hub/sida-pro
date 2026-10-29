import type { QuoteSnapshot, DailyPnlPeriod } from './shared'
import type { QuoteTickMap } from '@/realtime/useQuoteStream'

/**
 * WS 行情帧 → 本地行情表（2026-10-29 独立复审 F3）。
 *
 * ## 为什么单独抽成纯函数
 * 合并规则里最容易写错的一条是「价格与时刻能不能分开用」。写在 hook 的
 * `setQuotes(prev => …)` 闭包里既难测也容易被下一次重构悄悄改回去，故抽成纯函数，
 * 由 `useStocksData` 调用、由 `tests/lib/quote-ticks.test.ts` 钉死。
 *
 * ## 规则：价格与时刻必须**同源**
 * 后端 WS 下行帧的 payload 只有 `{price, change_pct, prev_close, name}`
 * （`src/web/api/quote_stream.py::_fetch_batch_quotes`）—— **没有行情源报时**。
 * 于是有两种情况：
 *  1. 帧里带了可靠的 `quote_time`（服务端补上时刻后的新协议/后端直连行情源）：
 *     价格、时刻、日期、`daily_pnl_period` 一起换 —— 这才是可判时效的一对；
 *  2. 帧里没带时刻（当前线上就是这种）：**价格照更，时刻明确清成未知**。
 *     旧实现把上一轮轮询的 `quote_time`/`quote_date`/`daily_pnl_period` 原样保留，
 *     等于"14:00 的新价配 09:31 的旧时刻"：既会让旧时刻替新价格背书（冒充实时），
 *     也会把当日盈亏算到上一轮（甚至上一交易日）的口径上。
 *     "拿不到新时刻" ≠ "时刻仍然有效"，所以这里的降级方向是 `null` + `unknown`，
 *     由 `freshnessFor` 判成 `unknown`（时点未知，不冒充实时）。
 *     唯一例外是**价格逐位没变**：那时旧时刻描述的还是同一个价（"110 @ 14:00"不会被
 *     一笔"现在还是 110"的推送推翻），配对仍然成立，保留即可 —— 这既诚实也避免了
 *     "价格几十秒不动，却每 5s 把时效打回未知"的噪声。
 *
 * 另外两条硬约束：
 *  - 绝不用本机时钟造一个时刻（假新鲜比没时刻更糟）；
 *  - 脏帧（价非有限数/非数值）整条忽略：不许清空已有价，也不许写入坏值。
 */
export interface QuoteTickTarget {
  symbol: string
  market?: string | null
}

const PERIODS: ReadonlySet<string> = new Set([
  'today',
  'previous_trading_day',
  'mixed',
  'unknown',
])

const asPeriod = (raw: unknown): DailyPnlPeriod =>
  PERIODS.has(String(raw ?? '')) ? (String(raw) as DailyPnlPeriod) : 'unknown'

const nonEmpty = (raw: unknown): string | null => {
  const text = typeof raw === 'string' ? raw.trim() : ''
  return text ? text : null
}

export function applyQuoteTicks(
  prev: Record<string, QuoteSnapshot>,
  targets: readonly QuoteTickTarget[],
  ticks: QuoteTickMap,
): Record<string, QuoteSnapshot> {
  const next = { ...prev }
  for (const target of targets) {
    const tick = ticks[target.symbol]
    if (!tick || typeof tick.price !== 'number' || !Number.isFinite(tick.price)) continue
    const key = `${target.market}:${target.symbol}`
    const carried = next[key]
    const frameTime = nonEmpty(tick.quote_time)
    // ① 帧自带可靠时刻 ⇒ 价格与时刻一起换（唯一"更新时效"的路径）。
    // ② 帧没带时刻，但价格与上一轮**已验证的那一对**逐位相同 ⇒ 那一对仍然成立：
    //    "110 出现在 14:00" 这件事不会被一笔"现在还是 110"的推送推翻，报价时刻依然有效。
    //    （这也是实际最常见的情形 —— 价格几十秒不变，没必要每 5s 把时效打回未知。）
    // ③ 价格变了却没有时刻 ⇒ 旧时刻属于旧价，**必须**判未知，绝不沿用。
    const pairStillValid =
      frameTime === null &&
      carried !== undefined &&
      carried.current_price === tick.price &&
      nonEmpty(carried.quote_time) !== null
    const quoteTime = frameTime ?? (pairStillValid ? carried.quote_time : null)
    next[key] = {
      current_price: tick.price,
      change_pct: tick.change_pct ?? null,
      // 有可靠时刻（或价格未变、原配对仍成立）⇒ 价格与时刻成对；否则时刻未知。
      quote_time: quoteTime,
      quote_date: frameTime !== null ? nonEmpty(tick.quote_date) : pairStillValid ? carried.quote_date : null,
      daily_pnl_period:
        frameTime !== null
          ? asPeriod(tick.daily_pnl_period)
          : pairStillValid
            ? carried.daily_pnl_period
            : 'unknown',
    }
  }
  return next
}
