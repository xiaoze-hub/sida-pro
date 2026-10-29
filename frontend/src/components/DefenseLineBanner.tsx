import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { fetchAPI } from '@sida/api'
import { usePausedInterval } from '@sida/base-ui'
import {
  currencySymbolForMarket,
  formatPriceWithCurrency,
  humanizeAgeMinutes,
  resolveDefenseState,
  sessionIsIdle,
  type DefenseState,
  type FreshnessReason,
  type MarketSessionInfo,
  type QuoteFreshness,
} from '@/lib/position-risk'
import { AlertOctagon, ClockAlert, Loader2, ShieldAlert, ShieldPlus } from 'lucide-react'
import { safeFixed } from '@/lib/format'
import { Button } from '@sida/base-ui/components/ui/button'
import { Input } from '@sida/base-ui/components/ui/input'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@sida/base-ui/components/ui/dialog'
import {
  notifyDefenseLinesChanged,
  onDefenseLinesChanged,
  onWindowDefenseLinesChanged,
} from '@/lib/defense-lines'

/**
 * 持仓防线击穿与止损预警条 (DefenseLineBanner)
 *
 * 核心职能:
 * 1. 展示**用户自己设的**防守线(positions.defense_price, 绝对价), 盘中跌破即强提醒;
 * 2. 用户没设 → 只给一条静态提示和"设置"入口, **不告警**;
 * 3. 同一标的被多个账户持有时,**逐账户**各说各的防线 —— 防线是账户级的纪律位。
 *
 * 方案 B 裁决(2026-10-23): 系统不再拿"成本价 × (1-8%)"冒充用户的纪律位。
 *
 * ## 2026-10-29 评审修的三类错判
 * ① 旧实现 `rows.find(p => p.symbol === symbol)` 只取**第一个**账户的 defense_price。
 *    同一只票主账户设 34、备用账户设 25、现价 33 时，告警条按 rows 顺序在「已击穿」和
 *    「防守线(安全)」之间来回翻，与持仓页清单互相打架。现在收集该标的在**所有账户**的持仓，
 *    逐条显示：账户名 + 该账户自己的防线 + 该账户自己的状态。不猜、不合并、不取一条。
 * ② `/quotes/{symbol}` 给的可能是上一交易日甚至更早的报价（字段是 `current_price`，
 *    旧代码读的 `price` 根本不存在，等于永远走"缺现价"降级 → 告警条从未真正工作）。
 *    现在读 `current_price` + `quote_time` + `daily_pnl_period`，并用**服务器市场墙钟**
 *    （`/stocks/markets/status` 的 `local_time`/`status`）判时刻层：报价不是当日、
 *    或当日但已过去太久、或时点根本无法证明时，只有「时效未证实」，**不**下击穿结论。
 * ③ **取价必须带市场**：旧实现只打 `/quotes/{symbol}`（后端默认 CN），港美股持仓永远
 *    拿不到价 → 永远是「缺现价」。现在按持仓行自己声明的 `market` 分组，逐市场取价；
 *    同一代码在不同市场（如 00700 / 0700.HK 与某个 A 股代码）互不串价。
 *    成本与「-5%/-8%/-10%」预设也一律按**账户那一行**的成本算，不再共用父组件传来的
 *    第一份成本（多账户成本不同时，预设会算成另一账户的价位）。
 *
 * 取价口径:
 * - 所有价格经 safeFixed / formatPriceWithCurrency 格式化(禁直接 toFixed, 遵守 R6 门禁);
 * - 缺现价时**显式降级**成一行说明, 绝不 return null 静默消失 ——
 *   "看不见的告警条"会被用户读成"我的防线没被击穿"。
 */

interface QuoteData {
  current_price?: number | null
  /** 兼容旧字段名（历史调用方），有则用。 */
  price?: number | null
  quote_date?: string | null
  quote_time?: string | null
  /** 时刻语义：`snapshot_refresh`=源快照刷新时间（腾讯 parts[30]），**不是**成交时刻。 */
  quote_time_kind?: string | null
  /** 日期证据来源：`daily_bar_close`=同标的日线收盘价配对（TQ，只证到日）。 */
  quote_date_basis?: string | null
  daily_pnl_period?: string | null
}

/** `/portfolio/summary` 里持仓行的最小子集(本组件只消费这几个字段)。 */
interface PositionRow {
  id: number
  symbol: string
  market?: string | null
  cost_price: number | null
  defense_price?: number | null
}

interface AccountLike {
  id: number
  name?: string | null
  positions?: PositionRow[]
}

interface PortfolioSummaryLike {
  accounts?: AccountLike[]
}

/** 一个账户在该标的上的持仓 + 它自己的防线 + 它自己的成本。 */
interface HeldLine {
  positionId: number | null
  accountId: string
  accountName: string
  market: string
  costPrice: number | null
  defensePrice: number | null
}

interface QuoteState {
  status: 'loading' | 'ok' | 'error'
  price: number | null
  period: string
  quoteDate: string | null
  quoteTime: string | null
  /** 时刻语义（源快照刷新 vs 成交）：UI 据此把"行情时间"改写成"源快照时间"。 */
  quoteTimeKind: string | null
  /** 日期证据来源：`daily_bar_close`=TQ 日线口径（只证到日）。 */
  quoteDateBasis: string | null
  /**
   * 这份读数是否**仍然被成功续期**（2026-10-29 独立复审 F2）。
   *
   * 告警条长期挂在页面上（持仓页整天开着）。旧实现只在挂载时取一次价，
   * 之后价与市场墙钟**一起停在 09:31** ⇒ "报价距今多久"恒为 0 ⇒ 旧价被判成实时破位。
   * 续期成功 ⇒ `true`；续期失败但手里还有旧价 ⇒ `false`（价照显示，但不许拿它判破位）。
   */
  verified: boolean
}

interface MarketStatusRow {
  code?: string
  status?: string | null
  local_time?: string | null
  /** 市场当地日期（`YYYY-MM-DD`，2026-10-29 起后端独立下发）。 */
  local_date?: string | null
}

interface PositionState {
  status: 'loading' | 'ok' | 'error'
  lines: HeldLine[]
  /**
   * 持仓行这份读数是否"仍然被成功续期"（2026-10-29 复审 B5）。
   *
   * 旧实现只在挂载时取一次持仓：别处改了/删了防线，告警条不知道。现在按
   * `DEFENSE_QUOTE_REFRESH_MS` 续期 + 订阅防线变更广播；**续期失败时必须置 false**：
   * 手里这份防线已无从证明是当前的，不许拿它出破位结论（保守降级，而不是继续报警）。
   */
  verified: boolean
}

const EMPTY_QUOTE: QuoteState = {
  status: 'loading',
  price: null,
  period: 'unknown',
  quoteDate: null,
  quoteTime: null,
  quoteTimeKind: null,
  quoteDateBasis: null,
  verified: true,
}

/**
 * 告警条自己的续期节奏（毫秒）—— **与页面「自动刷新」开关无关**。
 *
 * 防线告警是"到点必须说话"的东西：用户关掉自动刷新，不等于允许它拿上午 09:31 的价
 * 在 14:00 继续报「已击穿」。20s 与页面时效心跳同量级（远小于时效窗口的推断值）。
 */
export const DEFENSE_QUOTE_REFRESH_MS = 20_000

/**
 * 休市（收盘后/周末）时的报价续期间隔（毫秒）—— 2026-10-29 降频。
 *
 * 收盘后价格不再变化，每个开着的持仓页仍各自 20s 打一次 `/quotes/{symbol}` 只是纯流量
 * （行情源按分钟级限流，多用户并发最容易打 429）。只有服务器判定相关市场**全部**
 * `after_hours` / `closed` 时才降到 120s；`pre_market` / `break` 不降（马上要开盘）。
 *
 * 诚实声明：本仓库没有压测环境，**没有验证过**多用户下的实际 QPS；这里只保证
 * "休市时每个页面的取价次数降一个量级"。
 */
export const DEFENSE_IDLE_REFRESH_MS = 120_000

/** 未设置时的参考预设: 仅把**该账户**的成本价折算成绝对价给用户点选, 不落库、不告警。 */
const PRESETS = [
  { pct: -5, label: '-5%' },
  { pct: -8, label: '-8%' },
  { pct: -10, label: '-10%' },
] as const

function presetPrice(cost: number, pct: number): number {
  return Math.round(cost * (1 + pct / 100) * 100) / 100
}

function positivePrice(raw: unknown): number | null {
  const n = typeof raw === 'number' ? raw : Number(raw)
  return Number.isFinite(n) && n > 0 ? n : null
}

const normalizeMarket = (market: unknown): string =>
  String(market ?? 'CN').trim().toUpperCase() || 'CN'

const shell = 'mt-2 flex items-center justify-between gap-3 rounded border px-3 py-2 text-[12px]'
const lineShell = 'flex flex-wrap items-center gap-2'

/** 账户标签：多账户时必须能分清是哪一条防线。 */
function AccountTag({ name }: { name: string }) {
  return (
    <span className="rounded bg-s1 px-1.5 py-0.5 text-[12px] text-muted-foreground">{name}</span>
  )
}

/** 时效说明：只说证明得了的（报价时刻 + 距今多久），不写「实时」除非真能证明。 */
function freshnessNote(args: {
  freshness: QuoteFreshness
  reason: FreshnessReason
  quoteTime: string | null
  quoteDate: string | null
  quoteTimeKind: string | null
  ageMinutes: number | null
  marketStatus: string | null
}): string {
  const at = args.quoteTime ? args.quoteTime.slice(11, 16) : ''
  const age = humanizeAgeMinutes(args.ageMinutes)
  // 源只报"快照刷新时刻"（腾讯 parts[30]）：停牌股也会被刷新，所以措辞里不能出现"成交"。
  const atLabel = args.quoteTimeKind === 'snapshot_refresh'
    ? `源快照更新${at ? ` ${at}` : ''}（非成交时刻）`
    : ''
  if (args.freshness === 'current') {
    const head = args.marketStatus === 'trading' ? '交易中' : '非交易时段 · 最近报价'
    return [at ? `${head} ${at}` : head, age, atLabel].filter(Boolean).join(' · ')
  }
  if (args.reason === 'date_only') {
    // TQ 日线口径：日期证明得了、时刻证明不了。非交易时段才敢说"最近报价"，且标日期。
    const stamp = args.quoteDate ? `日线口径 ${args.quoteDate}` : '日线口径'
    if (args.freshness === 'session_frozen') {
      return `非交易时段 · 最近报价（${stamp}，无成交时刻）`
    }
    return `时点未知（源只提供日期 ${args.quoteDate ?? '——'}，未提供成交时刻）`
  }
  if (args.freshness === 'session_frozen') {
    return [`非交易时段 · 最近报价${at ? ` ${at}` : ''}`, age, atLabel].filter(Boolean).join(' · ')
  }
  if (args.reason === 'period') {
    const stamp = args.quoteDate ?? (args.quoteTime ? args.quoteTime.slice(0, 10) : '')
    return `历史报价 ${stamp || '（日期未知）'}${at ? ` ${at}` : ''}`
  }
  if (args.reason === 'intraday_age') {
    return [`滞后${age ? ` ${age}` : ''}`, at ? `报价 ${at}` : ''].filter(Boolean).join(' · ')
  }
  if (args.reason === 'no_time') return '时点未知（报价无时间）'
  if (args.reason === 'period_unknown') {
    return [`时点未知（无法确认报价属于当日）${at ? ` · 报价 ${at}` : ''}`, age]
      .filter(Boolean)
      .join(' · ')
  }
  if (args.reason === 'refresh_failed') return '时效未核实（刷新失败：显示的是最后一次成功取到的报价，不作时效证明）'
  if (args.reason === 'clock_skew') return '时点存疑（报价晚于市场时钟）'
  return '时点未证实（缺服务器市场时钟）'
}

export function DefenseLineBanner({
  symbol,
  cost,
  hasPosition,
}: {
  symbol: string
  cost: number | null
  hasPosition?: boolean
}) {
  const [quotesByMarket, setQuotesByMarket] = useState<Record<string, QuoteState>>({})
  const [sessions, setSessions] = useState<Record<string, MarketSessionInfo> | null>(null)
  const [position, setPosition] = useState<PositionState>({
    status: 'loading',
    lines: [],
    verified: true,
  })
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<HeldLine | null>(null)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  const active = !!hasPosition

  // 卸载后置 false：异步回调据此丢弃结果（避免卸载后 setState / 续期继续跑）。
  // 注意顺序：`loadPositions` / `loadSessions` / `loadQuotes` 的异步体都会读它，
  // 所以它必须声明在使用者之前（effect 执行时一定已初始化，这里只是避免 use-before-define）。
  const aliveRef = useRef(true)
  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
    }
  }, [])

  // 续期闸门：上一轮持仓行请求还没回来就不再叠一轮（广播 + 心跳可能同时到点）。
  const positionInFlightRef = useRef(false)
  /**
   * 取该标的在**所有账户**的持仓行（防线/成本来自持仓行本身，而不是父组件 props）。
   *
   * 2026-10-29 复审 B5：旧实现只在挂载时取一次 —— 别处改了防线（改/删/换账户）后，
   * 告警条仍按挂载时那份防线报警。现在首拉与续期走同一个 loader：
   *  - 防线变更广播（`notifyDefenseLinesChanged`，见 `@/lib/defense-lines`）立即触发；
   *  - `DEFENSE_QUOTE_REFRESH_MS` 心跳兜底（覆盖助手/脚本直接改库式改动）；
   *  - 失败时**保留旧读数但标记未核实**（`verified=false`），由判定层降级成"时效未核实"，
   *    不再拿过期防线出破位结论。
   */
  const loadPositions = useCallback(async () => {
    if (!active) {
      setPosition({ status: 'ok', lines: [], verified: true })
      return
    }
    if (positionInFlightRef.current) return
    positionInFlightRef.current = true
    try {
      // `cacheMode:'reload'` 是必须的（2026-10-29 复审 B5）：`fetchAPI` 对 GET 有 30s 内存缓存，
      // 续期/广播触发的重取若命中缓存，等于**又拿回同一份旧防线**，"续期"变成空动作。
      const res = await fetchAPI<PortfolioSummaryLike>('/portfolio/summary?include_quotes=false', {
        cacheMode: 'reload',
      })
      if (!aliveRef.current) return
      const lines: HeldLine[] = (res?.accounts || []).flatMap((acc) =>
        (acc.positions || [])
          .filter((p) => p.symbol === symbol)
          .map((p) => ({
            positionId: p.id ?? null,
            accountId: String(acc.id),
            accountName: String(acc.name ?? '账户'),
            market: normalizeMarket(p.market),
            costPrice: positivePrice(p.cost_price),
            defensePrice: positivePrice(p.defense_price),
          })),
      )
      // `ok` 但 lines 为空 = 这条持仓已经不在（被删/换账户）：渲染层明说"不在持仓中"，
      // 不继续用旧防线报警。`error` 只留给"取不到"。
      setPosition({ status: 'ok', lines, verified: true })
    } catch {
      if (!aliveRef.current) return
      setPosition((prev) =>
        // 有过读数 ⇒ 旧读数还在（价/防线都能陈列），但已无从核实：verified=false。
        // 从没读到过 ⇒ "未知"（不是"未核实"）：没有旧读数可核实，也没法判破位。
        prev.lines.length > 0
          ? { ...prev, verified: false }
          : { status: 'error', lines: [], verified: true },
      )
    } finally {
      positionInFlightRef.current = false
    }
  }, [active, symbol])

  // 挂载 / 切标的 / 持仓可见性变化 ⇒ 首拉。
  useEffect(() => {
    if (!active) {
      setPosition({ status: 'ok', lines: [], verified: true })
      setQuotesByMarket({})
      return
    }
    setPosition({ status: 'loading', lines: [], verified: true })
    void loadPositions()
  }, [loadPositions, active])

  // 防线变更广播（同页模块级订阅 + 跨标签页 window 事件）：别处保存/删除后立刻重取。
  useEffect(() => {
    const reload = () => {
      void loadPositions()
    }
    const offModule = onDefenseLinesChanged(reload)
    const offWindow = onWindowDefenseLinesChanged(reload)
    return () => {
      offModule()
      offWindow()
    }
  }, [loadPositions])

  // 服务器市场墙钟/状态：时效判定的「时刻层」只能用服务器给的事实，不读本机时钟。
  // 2026-10-29 独立复审 F2：这份墙钟必须能**续期**，且续期失败时不许沿用旧值 ——
  // 一个停在 09:31 的墙钟配上 09:31 的报价，就是"永远新鲜"的假象（判破位的直接原因）。
  const loadSessions = useCallback(async () => {
    if (!active) return
    try {
      // 同上：30s 内存缓存会让"墙钟续期"变成空动作（时钟停在缓存那一刻）。
      const rows = await fetchAPI<MarketStatusRow[]>('/stocks/markets/status', { cacheMode: 'reload' })
      if (!aliveRef.current) return
      const next: Record<string, MarketSessionInfo> = {}
      for (const row of Array.isArray(rows) ? rows : []) {
        if (!row?.code) continue
        next[normalizeMarket(row.code)] = {
          localTime: row.local_time ?? null,
          // 市场当地日期：日级一致性比对用（缺则判定层降级 period_unknown，绝不拿 HH:mm 自证）。
          localDate: row.local_date ?? null,
          status: row.status ?? null,
        }
      }
      setSessions(next)
    } catch {
      // 拿不到（最新的）服务器市场时钟 ⇒ 时效一律「未证实」：不用旧墙钟替它作证。
      if (!aliveRef.current) return
      setSessions(null)
    }
  }, [active])

  // 逐**市场**取价：`/quotes/{symbol}` 的 market 参数决定后端按哪个市场解析代码，
  // 不传就是 CN —— 港股/美股持仓会永远拿不到价（旧实现的港美"缺现价"根因）。
  const markets = useMemo(() => {
    const set = new Set(position.lines.map((line) => line.market))
    return [...set].sort()
  }, [position.lines])
  const marketsKey = markets.join(',')

  // 续期闸门：上一轮还没回来就不再叠一轮（心跳 + 标的变化可能同时触发）。
  const quotesInFlightRef = useRef(false)
  const loadQuotes = useCallback(
    async (targets: readonly string[]) => {
      if (!active || targets.length === 0) return
      if (quotesInFlightRef.current) return
      quotesInFlightRef.current = true
      try {
        await Promise.all(
          targets.map(async (market) => {
            try {
              // 30s 内存缓存会让 20s 续期变成空动作 —— 价必须真取（B1/F2 的续期才有意义）。
              const res = await fetchAPI<QuoteData>(
                `/quotes/${encodeURIComponent(symbol)}?market=${encodeURIComponent(market)}`,
                { cacheMode: 'reload' },
              )
              if (!aliveRef.current) return
              const p = positivePrice(res?.current_price ?? res?.price)
              setQuotesByMarket((prev) => ({
                ...prev,
                [market]: {
                  status: p === null ? 'error' : 'ok',
                  price: p,
                  period: String(res?.daily_pnl_period ?? 'unknown'),
                  quoteDate: res?.quote_date ?? null,
                  quoteTime: res?.quote_time ?? null,
                  quoteTimeKind: res?.quote_time_kind ?? null,
                  quoteDateBasis: res?.quote_date_basis ?? null,
                  verified: true,
                },
              }))
            } catch {
              if (!aliveRef.current) return
              // 续期失败：旧价留着给人看（别把读数藏起来），但**标记未核实** ——
              // 手里这份快照已无从证明是当下的，不许拿它出破位结论。
              setQuotesByMarket((prev) => {
                const old = prev[market]
                return {
                  ...prev,
                  [market]:
                    old && old.price !== null
                      ? { ...old, status: 'error', verified: false }
                      : { ...EMPTY_QUOTE, status: 'error' },
                }
              })
            }
          }),
        )
      } finally {
        quotesInFlightRef.current = false
      }
    },
    [active, symbol],
  )

  useEffect(() => {
    void loadSessions()
  }, [loadSessions])

  useEffect(() => {
    if (!active || markets.length === 0) {
      setQuotesByMarket({})
      return
    }
    setQuotesByMarket(
      Object.fromEntries(markets.map((m) => [m, { ...EMPTY_QUOTE }])) as Record<string, QuoteState>,
    )
    void loadQuotes(markets)
    // marketsKey 已经把 markets 的内容编码进来了（数组身份每次渲染都变）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, active, marketsKey])

  // 续期：价 + 市场墙钟 + 持仓行（防线）一起刷 —— 只刷其中一个都会让结论失真：
  // 缺价没结论、缺钟没法判时刻、缺持仓行则可能拿着一条已经被改掉/删掉的防线继续报警。
  // 页签隐藏由 `usePausedInterval` 停表，回前台立即补一次，卸载自动清定时器。
  const marketsRef = useRef(markets)
  marketsRef.current = markets
  const refreshTick = useCallback(() => {
    void loadSessions()
    void loadQuotes(marketsRef.current)
    void loadPositions()
  }, [loadPositions, loadQuotes, loadSessions])

  /**
   * 是否降频到「休市节奏」（2026-10-29）：只有当**所有**持仓市场都被服务器判成
   * `after_hours` / `closed` 时才降。会话表缺一个市场、或状态未知/盘前/午休
   * ⇒ 一律按盘中 20s 取（漏判比多取一轮更贵）。
   *
   * 依赖 `sessions` 的身份：每次 `loadSessions` 成功都会换新对象，但 `idle` 只在
   * 布尔值翻转时改变 ⇒ 下面的 `refreshMs` 稳定，不会每 20s 重挂一次定时器。
   */
  const idle = useMemo(() => {
    if (!sessions || markets.length === 0) return false
    const infos = markets.map((market) => sessions[market])
    if (infos.some((info) => !info)) return false
    return infos.every((info) => sessionIsIdle(info?.status))
  }, [sessions, markets])
  const refreshMs = idle ? DEFENSE_IDLE_REFRESH_MS : DEFENSE_QUOTE_REFRESH_MS
  usePausedInterval(refreshTick, refreshMs, {
    // marketsKey 而非 markets：数组身份每渲染都变；refreshMs 进依赖 ⇒ 只在节奏切换时重挂。
    // eslint-disable-next-line react-hooks/exhaustive-deps
    deps: [active, symbol, marketsKey, refreshMs],
    immediate: false,
  })

  const saveDefense = useCallback(
    async (positionId: number | null, value: number | null) => {
      if (positionId == null) {
        setSaveError('持仓信息未就绪，无法保存')
        return
      }
      setSaving(true)
      setSaveError(null)
      try {
        const res = await fetchAPI<{ defense_price?: number | null }>(
          `/positions/${positionId}/defense`,
          { method: 'PUT', body: JSON.stringify({ defense_price: value }) },
        )
        const saved = positivePrice(res?.defense_price)
        setPosition((prev) => ({
          ...prev,
          status: 'ok',
          verified: true,
          lines: prev.lines.map((line) =>
            line.positionId === positionId ? { ...line, defensePrice: saved } : line,
          ),
        }))
        // 广播给其他页面/标签页（B5）：这里保存成功，那边挂着的告警条要立刻重取，
        // 否则它会继续按旧防线报警。传值没有意义（别人不知道我们改成了什么），只传"变了"。
        notifyDefenseLinesChanged()
        setDialogOpen(false)
      } catch {
        setSaveError('保存失败，请重试')
      } finally {
        setSaving(false)
      }
    },
    [],
  )

  const openDialog = (line: HeldLine, seed: number | null) => {
    setEditing(line)
    setDraft(seed != null ? safeFixed(seed, 2, '') : '')
    setSaveError(null)
    setDialogOpen(true)
  }

  const submitDraft = () => {
    const v = Number(draft)
    if (!draft.trim() || !Number.isFinite(v) || v <= 0) {
      setSaveError('请输入大于 0 的绝对价格')
      return
    }
    void saveDefense(editing?.positionId ?? null, Math.round(v * 100) / 100)
  }

  /** 编辑中的账户成本（缺成本时退回父组件的成本，并在文案里说明是回退值）。 */
  const editingCost =
    positivePrice(editing?.costPrice) ?? positivePrice(cost) ?? null
  const editingCostIsFallback = positivePrice(editing?.costPrice) === null && editingCost !== null
  const editingUnit = currencySymbolForMarket(editing?.market)

  const dialog = (
    <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>
            设置防守线
            {editing?.accountName ? ` · ${editing.accountName}` : ''}
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-3 text-[12px]">
          <div className="text-muted-foreground">
            成本价{' '}
            <span className="tabular-nums">
              {editingCost === null ? '未知' : safeFixed(editingCost, 2)}
            </span>{' '}
            {editingUnit || '（币种未知）'}
            {editingCostIsFallback ? '（该账户成本缺失，此处为页面显示值）' : ''}。
            防守线是你定的绝对价，跌到即提示执行纪律；不设则不告警。
          </div>
          <div className="flex items-center gap-2">
            <Input
              type="number"
              step="0.01"
              inputMode="decimal"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={editingCost === null ? '' : `如 ${safeFixed(presetPrice(editingCost, -8), 2)}`}
              className="tabular-nums"
              data-testid="defense-line-input"
            />
            <span className="shrink-0 text-muted-foreground">{editingUnit || '价格'}</span>
          </div>
          {editingCost !== null && (
            <div className="flex flex-wrap gap-1.5">
              {PRESETS.map((p) => (
                <Button
                  key={p.pct}
                  type="button"
                  size="sm"
                  variant="outline"
                  className="h-7 px-2 text-[12px] tabular-nums"
                  onClick={() => setDraft(safeFixed(presetPrice(editingCost, p.pct), 2, ''))}
                >
                  {p.label} · {safeFixed(presetPrice(editingCost, p.pct), 2)}
                </Button>
              ))}
            </div>
          )}
          {saveError && <div className="text-rose-400">{saveError}</div>}
          <div className="flex items-center justify-between gap-2 pt-1">
            <div>
              {editing?.defensePrice != null && (
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="h-7 px-2 text-[12px] text-muted-foreground"
                  disabled={saving}
                  onClick={() => void saveDefense(editing?.positionId ?? null, null)}
                >
                  清除防守线
                </Button>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="h-7 px-2 text-[12px]"
                onClick={() => setDialogOpen(false)}
              >
                取消
              </Button>
              <Button
                type="button"
                size="sm"
                className="h-7 px-2 text-[12px]"
                disabled={saving}
                onClick={submitDraft}
                data-testid="defense-line-save"
              >
                {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : '保存'}
              </Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )

  if (!active) return null
  const anyQuoteLoading =
    position.status === 'loading' ||
    markets.some((m) => (quotesByMarket[m]?.status ?? 'loading') === 'loading')
  if (anyQuoteLoading) return null

  // ① 持仓读取失败: 说"未知", 不冒充"未设置"。
  if (position.status === 'error') {
    return (
      <div
        className={`${shell} border-border bg-s1 text-muted-foreground`}
        data-testid="defense-line-unknown"
      >
        <span>
          {position.verified
            ? '防守线状态未知（持仓读取失败）'
            : '防守线状态未核实（持仓读取失败，不按旧读数判破位）'}
        </span>
      </div>
    )
  }
  // ①b 持仓行不在了（被删除 / 换到别的账户）：明说，不继续拿旧防线报警。
  if (position.lines.length === 0) {
    return (
      <div
        className={`${shell} border-border bg-s1 text-muted-foreground`}
        data-testid="defense-line-unknown"
      >
        <span>该持仓已不在持仓中（防守线不适用）；页面数据更新后本条会消失</span>
      </div>
    )
  }

  const lines = position.lines
  const multi = lines.length > 1
  const allUnset = lines.every((l) => l.defensePrice === null)

  /** 该账户那一行的成本（缺成本时不拿别的账户顶替，如实说未知）。 */
  const costOf = (line: HeldLine) => line.costPrice ?? positivePrice(cost)
  const costNote = (line: HeldLine): string => {
    const c = costOf(line)
    if (c === null) return '成本未知'
    const fallback = line.costPrice === null
    return `成本 ${safeFixed(c, 2)}${fallback ? '（页面显示值）' : ''}`
  }
  const unitOf = (line: HeldLine) => currencySymbolForMarket(line.market) || '价格'

  // ② 都没设防线: 静态提示行(不是警告条) + 设置入口(每账户各自一条)。
  if (allUnset) {
    return (
      <>
        <div
          className={`${shell} border-border bg-s1 text-muted-foreground ${multi ? 'flex-col items-stretch' : ''}`}
          data-testid="defense-line-unset"
        >
          {multi && (
            <div className="text-[12px] opacity-80">
              {lines.length} 个账户持有 {symbol}，都未设置防守线
            </div>
          )}
          {lines.map((line) => {
            const c = costOf(line)
            return (
              <div
                key={line.accountId || line.positionId}
                className={lineShell}
                data-testid={`defense-line-account-${line.accountId}`}
              >
                {multi && <AccountTag name={line.accountName} />}
                <ShieldPlus className="h-4 w-4 shrink-0 text-role-system" />
                <span className="font-medium text-foreground">未设置防守线</span>
                <span>
                  {c === null ? (
                    '成本未知，仅可手动填绝对价'
                  ) : (
                    <>
                      {costNote(line)}，参考 -8% ={' '}
                      <span className="tabular-nums">{safeFixed(presetPrice(c, -8), 2)}</span>{' '}
                      {unitOf(line)}
                    </>
                  )}
                  <span className="ml-1 text-[12px] opacity-70">(系统参考值，非你的纪律线)</span>
                </span>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="ml-auto h-7 shrink-0 px-2 text-[12px]"
                  onClick={() => openDialog(line, c === null ? null : presetPrice(c, -8))}
                  data-testid={`defense-line-set-${line.accountId}`}
                >
                  设置
                </Button>
              </div>
            )
          })}
        </div>
        {dialog}
      </>
    )
  }

  /** 单条防线：用共享判定（含行情时效闸门：日期层 + 服务器市场墙钟时刻层）。 */
  const renderLine = (line: HeldLine) => {
    const quote = quotesByMarket[line.market] ?? EMPTY_QUOTE
    const session = sessions?.[line.market] ?? null
    const verdict = resolveDefenseState({
      price: quote.price,
      defense: line.defensePrice,
      period: quote.period,
      quoteTime: quote.quoteTime,
      quoteDate: quote.quoteDate,
      market: session,
      // 续期失败（价或持仓行任一条）⇒ 这份快照不再能证明"是当下的"，一律判时效未核实。
      verified: quote.verified && position.verified,
    })
    const state: DefenseState = verdict.state
    const breached = state === 'breached'
    const near = state === 'near'
    const stale = state === 'stale'
    const unset = state === 'unset'
    const missingPrice = quote.price === null
    const note = missingPrice
      ? `该市场（${line.market}）无可用现价`
      : freshnessNote({
          freshness: verdict.freshness,
          reason: verdict.reason,
          quoteTime: quote.quoteTime,
          quoteDate: quote.quoteDate,
          quoteTimeKind: quote.quoteTimeKind,
          ageMinutes: verdict.ageMinutes,
          marketStatus: String(session?.status ?? '').trim().toLowerCase() || null,
        })
    const c = costOf(line)
    const lossPct =
      !missingPrice && quote.price !== null && c !== null && c > 0
        ? ((quote.price - c) / c) * 100
        : null
    return {
      state,
      breached,
      near,
      stale,
      unset,
      missingPrice,
      content: (
        <div
          key={line.accountId || line.positionId}
          className={lineShell}
          data-testid={`defense-line-account-${line.accountId}`}
          data-state={state}
          data-freshness={verdict.freshness}
          data-market={line.market}
        >
          {multi && <AccountTag name={line.accountName} />}
          {unset ? (
            <ShieldPlus className="h-4 w-4 shrink-0 text-role-system" />
          ) : missingPrice ? (
            <ShieldAlert className="h-4 w-4 shrink-0" />
          ) : breached ? (
            <AlertOctagon className="h-4 w-4 shrink-0 text-rose-400" />
          ) : stale ? (
            <ClockAlert className="h-4 w-4 shrink-0 text-role-watch" />
          ) : (
            <ShieldAlert
              className={`h-4 w-4 shrink-0 ${near ? 'text-amber-400' : 'text-role-system'}`}
            />
          )}
          <span className="font-semibold">
            {unset
              ? '未设置防守线'
              : missingPrice
                ? '防守线无法计算（缺现价）'
                : breached
                  ? '已击穿你的防守线'
                  : stale
                    ? '时效未证实 · 未判破位'
                    : near
                      ? `接近你的防守线（${safeFixed(verdict.distancePct, 2)}%）`
                      : '防守线'}
          </span>
          <span className="tabular-nums">
            {line.defensePrice === null
              ? '未设'
              : `防线 ${formatPriceWithCurrency(line.defensePrice, line.market)}`}
            {quote.price !== null && ` · 现价 ${formatPriceWithCurrency(quote.price, line.market)}`}
            {lossPct != null && ` · 浮动 ${safeFixed(lossPct, 2)}%`}
            {!breached &&
              !stale &&
              verdict.distancePct != null &&
              ` · 距防线 ${safeFixed(verdict.distancePct, 2)}%`}
          </span>
          <span className="text-[12px] opacity-70">{costNote(line)}</span>
          <span className="text-[12px] opacity-70">{note}</span>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="ml-auto h-7 shrink-0 px-2 text-[12px] opacity-80"
            onClick={() => openDialog(line, line.defensePrice)}
          >
            {unset ? '设置' : '修改'}
          </Button>
        </div>
      ),
    }
  }

  const rendered = lines.map(renderLine)
  const anyBreached = rendered.some((r) => r.breached)
  const anyStale = rendered.some((r) => r.stale)
  const allMissingPrice = rendered.every((r) => r.missingPrice)
  const toneOf = (state: DefenseState): string =>
    state === 'breached'
      ? 'border-rose-500/60 bg-rose-950/25 text-rose-100'
      : state === 'near'
        ? 'border-amber-500/50 bg-amber-950/20 text-amber-200'
        : state === 'stale'
          ? 'border-border bg-s1 text-role-watch'
          : 'border-border bg-s1 text-muted-foreground'

  // ③ 每条线都取不到现价: 显式降级, 不静默消失。
  if (allMissingPrice) {
    return (
      <>
        <div
          className={`${shell} border-border bg-s1 text-role-watch ${multi ? 'flex-col items-stretch' : ''}`}
          data-testid="defense-line-degraded"
        >
          {rendered.map((r) => r.content)}
        </div>
        {dialog}
      </>
    )
  }

  // ④ 单账户且时效未证实: 单独一档, 明说"未判破位"。
  if (!multi && anyStale) {
    return (
      <>
        <div
          className={`${shell} border-border bg-s1 text-role-watch`}
          data-testid="defense-line-stale"
        >
          {rendered.map((r) => r.content)}
        </div>
        {dialog}
      </>
    )
  }

  const shellTone = anyBreached
    ? toneOf('breached')
    : anyStale
      ? toneOf('stale')
      : 'border-border bg-s1 text-muted-foreground'
  const multiTone = rendered.some((r) => r.breached)
    ? toneOf('breached')
    : rendered.some((r) => r.stale)
      ? toneOf('stale')
      : rendered.some((r) => r.near)
        ? toneOf('near')
        : 'border-border bg-s1 text-muted-foreground'

  // ⑤ 多账户: 一个账户一条, 不做任何合并。
  if (multi) {
    const distinctDefenses = new Set(lines.map((l) => l.defensePrice)).size
    const distinctCosts = new Set(lines.map((l) => l.costPrice)).size
    return (
      <>
        <div
          className={`${shell} flex-col items-stretch ${multiTone}`}
          data-testid="defense-line-multi"
        >
          <div className="text-[12px] opacity-80">
            {lines.length} 个账户持有 {symbol}
            {distinctDefenses > 1 ? '，各自的防线不同（分行显示，不合并）' : '，防线一致'}
            {distinctCosts > 1 ? '；成本也按账户分别计算' : ''}
          </div>
          {rendered.map((r) => r.content)}
        </div>
        {dialog}
      </>
    )
  }

  // ⑥ 单账户常态: 一条告警条。
  const single = rendered[0]
  return (
    <>
      <div className={`${shell} ${shellTone}`} data-testid="defense-line-banner">
        {single.content}
      </div>
      {dialog}
    </>
  )
}
