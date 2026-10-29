import { useCallback, useEffect, useMemo, useState } from 'react'
import { fetchAPI } from '@sida/api'
import {
  currencySymbolForMarket,
  formatPriceWithCurrency,
  humanizeAgeMinutes,
  resolveDefenseState,
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
}

interface MarketStatusRow {
  code?: string
  status?: string | null
  local_time?: string | null
}

interface PositionState {
  status: 'loading' | 'ok' | 'error'
  lines: HeldLine[]
}

const EMPTY_QUOTE: QuoteState = {
  status: 'loading',
  price: null,
  period: 'unknown',
  quoteDate: null,
  quoteTime: null,
}

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
  ageMinutes: number | null
  marketStatus: string | null
}): string {
  const at = args.quoteTime ? args.quoteTime.slice(11, 16) : ''
  const age = humanizeAgeMinutes(args.ageMinutes)
  if (args.freshness === 'current') {
    const head = args.marketStatus === 'trading' ? '交易中' : '非交易时段 · 最近报价'
    return [at ? `${head} ${at}` : head, age].filter(Boolean).join(' · ')
  }
  if (args.freshness === 'session_frozen') {
    return [`非交易时段 · 最近报价${at ? ` ${at}` : ''}`, age].filter(Boolean).join(' · ')
  }
  if (args.reason === 'period') {
    const stamp = args.quoteDate ?? (args.quoteTime ? args.quoteTime.slice(0, 10) : '')
    return `历史报价 ${stamp || '（日期未知）'}${at ? ` ${at}` : ''}`
  }
  if (args.reason === 'intraday_age') {
    return [`滞后${age ? ` ${age}` : ''}`, at ? `报价 ${at}` : ''].filter(Boolean).join(' · ')
  }
  if (args.reason === 'no_time') return '时点未知（报价无时间）'
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
  const [position, setPosition] = useState<PositionState>({ status: 'loading', lines: [] })
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<HeldLine | null>(null)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  const active = !!hasPosition

  // 防线/成本来自持仓行本身(而非父组件 props): 只有持仓行知道"用户到底设没设过"、
  // 以及"这个账户的成本是多少"。同标的多账户全都要取回来 —— 取第一条就是旧 bug。
  useEffect(() => {
    let cancelled = false
    if (!active) {
      setPosition({ status: 'ok', lines: [] })
      setQuotesByMarket({})
      return
    }
    setPosition({ status: 'loading', lines: [] })
    fetchAPI<PortfolioSummaryLike>('/portfolio/summary?include_quotes=false')
      .then((res) => {
        if (cancelled) return
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
        setPosition({ status: lines.length > 0 ? 'ok' : 'error', lines })
      })
      .catch(() => {
        if (!cancelled) setPosition({ status: 'error', lines: [] })
      })
    return () => {
      cancelled = true
    }
  }, [symbol, active])

  // 服务器市场墙钟/状态：时效判定的「时刻层」只能用服务器给的事实，不读本机时钟。
  useEffect(() => {
    if (!active) return
    let cancelled = false
    fetchAPI<MarketStatusRow[]>('/stocks/markets/status')
      .then((rows) => {
        if (cancelled) return
        const next: Record<string, MarketSessionInfo> = {}
        for (const row of Array.isArray(rows) ? rows : []) {
          const code = normalizeMarket(row?.code)
          if (!row?.code) continue
          next[code] = { localTime: row.local_time ?? null, status: row.status ?? null }
        }
        setSessions(next)
      })
      .catch(() => {
        // 拿不到服务器市场时钟 ⇒ 时效一律「未证实」（保守），不让告警条自己猜。
        if (!cancelled) setSessions({})
      })
    return () => {
      cancelled = true
    }
  }, [active])

  // 逐**市场**取价：`/quotes/{symbol}` 的 market 参数决定后端按哪个市场解析代码，
  // 不传就是 CN —— 港股/美股持仓会永远拿不到价（旧实现的港美"缺现价"根因）。
  const markets = useMemo(() => {
    const set = new Set(position.lines.map((line) => line.market))
    return [...set].sort()
  }, [position.lines])
  const marketsKey = markets.join(',')

  useEffect(() => {
    let cancelled = false
    if (!active || markets.length === 0) {
      setQuotesByMarket({})
      return
    }
    setQuotesByMarket(
      Object.fromEntries(markets.map((m) => [m, { ...EMPTY_QUOTE }])) as Record<string, QuoteState>,
    )
    for (const market of markets) {
      fetchAPI<QuoteData>(`/quotes/${encodeURIComponent(symbol)}?market=${encodeURIComponent(market)}`)
        .then((res) => {
          if (cancelled) return
          const p = positivePrice(res?.current_price ?? res?.price)
          setQuotesByMarket((prev) => ({
            ...prev,
            [market]: {
              status: p === null ? 'error' : 'ok',
              price: p,
              period: String(res?.daily_pnl_period ?? 'unknown'),
              quoteDate: res?.quote_date ?? null,
              quoteTime: res?.quote_time ?? null,
            },
          }))
        })
        .catch(() => {
          if (cancelled) return
          setQuotesByMarket((prev) => ({ ...prev, [market]: { ...EMPTY_QUOTE, status: 'error' } }))
        })
    }
    return () => {
      cancelled = true
    }
    // marketsKey 已经把 markets 的内容编码进来了（数组身份每次渲染都变）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, active, marketsKey])

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
          lines: prev.lines.map((line) =>
            line.positionId === positionId ? { ...line, defensePrice: saved } : line,
          ),
        }))
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
  if (position.status === 'error' || position.lines.length === 0) {
    return (
      <div
        className={`${shell} border-border bg-s1 text-muted-foreground`}
        data-testid="defense-line-unknown"
      >
        <span>防守线状态未知（持仓读取失败）</span>
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
      market: session,
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
