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
 * 后端 WS 下行帧的 payload 自 2026-10-29 起带 `quote_time` / `quote_date` /
 * `daily_pnl_period`（`src/web/api/quote_stream.py::_proven_quote_stamp`），值只来自
 * **行情源报文自带的报时**（腾讯 parts[30]）；源不报时就是 `null` + `unknown`。
 * 于是有两种情况：
 *  1. 帧里带了可靠的 `quote_time`（后端从**行情源报文**透传的报时）：价格、时刻、日期、
 *     `daily_pnl_period` 一起换 —— 这才是可判时效的一对；
 *  2. 帧里没带时刻（源本身不报时 / 降级源）：**价格照更，时刻明确清成未知**。
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
    const frameDate = nonEmpty(tick.quote_date)
    // 2026-10-29 F4: 帧里的**日期**与时刻同等算「同一次抓取的证据」——
    // TQ 没有报时字段，但后端用同标的日线收盘价配对证明了日期；只认时刻会把
    // TQ 的当日盈亏在 WS 路径上又打回 unknown（20s 轮询那条路是有日期的，两条不一致）。
    const frameProven = frameTime !== null || frameDate !== null
    const nextChangePct = tick.change_pct ?? null
    // ① 帧自带可靠时刻 ⇒ 价格与时刻一起换（唯一"更新时效"的路径）。
    // ② 帧没带时刻，但价格与上一轮**已验证的那一对**逐位相同、**涨跌幅也没有新信息** ⇒
    //    那一对仍然成立："110 出现在 14:00" 这件事不会被一笔"现在还是 110"的推送推翻。
    //    （这也是实际最常见的情形 —— 价格几十秒不变，没必要每 5s 把时效打回未知。）
    // ③ **价格没变但涨跌幅变了** ⇒ 帧带来的是"另一段时间"的信息（典型：隔日开盘同价、
    //    新的当日涨跌幅）。旧日期/期间属于旧的那一天，绝不能给新的涨跌幅作证 —— 否则
    //    「昨天 15:00 + 昨天判的 today + 今天的 change_pct」会一路被下游当今日盈亏（B3）。
    // ④ 价格变了却没有时刻 ⇒ 旧时刻属于旧价，**必须**判未知，绝不沿用。
    const sameChangePct = (carried?.change_pct ?? null) === nextChangePct
    const carriedProven =
      nonEmpty(carried?.quote_time) !== null || nonEmpty(carried?.quote_date) !== null
    const pairStillValid =
      !frameProven &&
      carried !== undefined &&
      carried.current_price === tick.price &&
      carriedProven &&
      sameChangePct
    const quoteTime = frameProven ? frameTime : pairStillValid ? carried.quote_time : null
    next[key] = {
      current_price: tick.price,
      change_pct: nextChangePct,
      // 有可靠时刻（或价格/涨跌幅都没变、原配对仍成立）⇒ 价格与时刻成对；否则时刻未知。
      quote_time: quoteTime,
      quote_date: frameProven
        ? frameDate
        : pairStillValid
          ? carried.quote_date
          : null,
      // 时刻语义/日期证据来源跟着**同一份**证据走：帧没给就清空，掉落回旧值会让
      // "TQ 日线口径"被标成"腾讯快照刷新"。
      quote_time_kind: frameProven
        ? nonEmpty(tick.quote_time_kind)
        : pairStillValid
          ? carried.quote_time_kind ?? null
          : null,
      quote_date_basis: frameProven
        ? nonEmpty(tick.quote_date_basis)
        : pairStillValid
          ? carried.quote_date_basis ?? null
          : null,
      daily_pnl_period: frameProven
        ? asPeriod(tick.daily_pnl_period)
        : pairStillValid
          ? carried.daily_pnl_period
          : 'unknown',
    }
  }
  return next
}

/**
 * 本地有效期到期后的降级（2026-10-29 复审 B1）：**价格留着陈列，时效证据全部作废**。
 *
 * 为什么需要：行情快照的"日期层"（`daily_pnl_period` / `quote_date`）只由轮询刷新。
 * 如果轮询挂死/长时间失败，`quotes` 里那条快照会一直躺着，而 WS tick 只更价 ——
 * 于是它看起来"刚刚被写过"，实际日期与时刻是几小时甚至隔日的。这里把证据清掉：
 *   - `quote_time` / `quote_date` → `null`、`daily_pnl_period` → `unknown`
 *     ⇒ 下游 `freshnessFor` 判 `unknown`（时点未证实），绝不当实时；
 *   - `change_pct` → `null`（它可能是上一交易日的涨跌幅，不配陈列在这个价旁边）；
 *   - `current_price` 保留（读数仍可核对，只是"什么时候的价"要标未知）。
 */
export function expireQuoteSnapshots(
  prev: Record<string, QuoteSnapshot>,
): Record<string, QuoteSnapshot> {
  const next: Record<string, QuoteSnapshot> = {}
  for (const [key, snapshot] of Object.entries(prev)) {
    next[key] = {
      current_price: snapshot.current_price,
      change_pct: null,
      quote_time: null,
      quote_date: null,
      quote_time_kind: null,
      quote_date_basis: null,
      daily_pnl_period: 'unknown',
    }
  }
  return next
}
