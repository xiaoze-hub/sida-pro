import { useCallback, useEffect, useMemo, useState } from 'react'
import { fetchAPI } from '@sida/api'
import { resolveDefenseState, type DefenseState } from '@/lib/position-risk'
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
 * ## 2026-10-29 评审修的两个错判
 * ① 旧实现 `rows.find(p => p.symbol === symbol)` 只取**第一个**账户的 defense_price。
 *    同一只票主账户设 34、备用账户设 25、现价 33 时，告警条按 rows 顺序在「已击穿」和
 *    「防守线(安全)」之间来回翻，与持仓页清单互相打架。现在收集该标的在**所有账户**的持仓，
 *    逐条显示：账户名 + 该账户自己的防线 + 该账户自己的状态。不猜、不合并、不取一条。
 * ② `/quotes/{symbol}` 给的可能是上一交易日甚至更早的报价（字段是 `current_price`，
 *    旧代码读的 `price` 根本不存在，等于永远走"缺现价"降级 → 告警条从未真正工作）。
 *    现在读 `current_price` + `daily_pnl_period`：报价不是当日时只有「行情滞后」，
 *    **不**下击穿结论，并给出真实行情日期与时间 —— 旧报价当实时价判破位，等于拿隔日
 *    (或隔周)的价位说"你现在已经跌破纪律位"。
 *
 * 取价口径:
 * - 所有价格经 safeFixed 格式化(禁直接 toFixed, 遵守 R6 门禁);
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
  cost_price: number
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

/** 一个账户在该标的上的持仓 + 它自己的防线。 */
interface HeldLine {
  positionId: number | null
  accountId: string
  accountName: string
  defensePrice: number | null
}

interface PositionState {
  status: 'loading' | 'ok' | 'error'
  lines: HeldLine[]
}

interface QuoteState {
  status: 'loading' | 'ok' | 'error'
  price: number | null
  period: string
  quoteDate: string | null
  quoteTime: string | null
}

/** 未设置时的参考预设: 仅把成本价折算成绝对价给用户点选, 不落库、不告警。 */
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

const shell = 'mt-2 flex items-center justify-between gap-3 rounded border px-3 py-2 text-[12px]'
const lineShell = 'flex flex-wrap items-center gap-2'

/** 账户标签：多账户时必须能分清是哪一条防线。 */
function AccountTag({ name }: { name: string }) {
  return (
    <span className="rounded bg-s1 px-1.5 py-0.5 text-[12px] text-muted-foreground">{name}</span>
  )
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
  const [quote, setQuote] = useState<QuoteState>({
    status: 'loading',
    price: null,
    period: 'unknown',
    quoteDate: null,
    quoteTime: null,
  })
  const [position, setPosition] = useState<PositionState>({ status: 'loading', lines: [] })
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState<HeldLine | null>(null)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  const active = !!hasPosition && !!cost && cost > 0
  const costVal = cost as number

  // 防线来自持仓本身(而非父组件 props): 只有持仓行知道"用户到底设没设过"。
  // 同标的多账户全都要取回来 —— 取第一条就是旧 bug。
  useEffect(() => {
    let cancelled = false
    if (!active) {
      setPosition({ status: 'ok', lines: [] })
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

  useEffect(() => {
    let cancelled = false
    if (!active) {
      setQuote({
        status: 'loading',
        price: null,
        period: 'unknown',
        quoteDate: null,
        quoteTime: null,
      })
      return
    }
    setQuote({
      status: 'loading',
      price: null,
      period: 'unknown',
      quoteDate: null,
      quoteTime: null,
    })
    fetchAPI<QuoteData>(`/quotes/${encodeURIComponent(symbol)}`)
      .then((res) => {
        if (cancelled) return
        // `/quotes/{symbol}` 现价字段是 `current_price`（旧代码读 `price` 恒 undefined）。
        const p = positivePrice(res?.current_price ?? res?.price)
        if (p === null) {
          // 响应 200 但没有可用价: 与请求失败同义(都算"缺现价")。
          setQuote({
            status: 'error',
            price: null,
            period: 'unknown',
            quoteDate: res?.quote_date ?? null,
            quoteTime: res?.quote_time ?? null,
          })
          return
        }
        setQuote({
          status: 'ok',
          price: p,
          period: String(res?.daily_pnl_period ?? 'unknown'),
          quoteDate: res?.quote_date ?? null,
          quoteTime: res?.quote_time ?? null,
        })
      })
      .catch(() => {
        if (!cancelled) {
          setQuote({
            status: 'error',
            price: null,
            period: 'unknown',
            quoteDate: null,
            quoteTime: null,
          })
        }
      })
    return () => {
      cancelled = true
    }
  }, [symbol, active])

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

  const staleLabel = useMemo(() => {
    const stamp = quote.quoteDate ?? (quote.quoteTime ? quote.quoteTime.slice(0, 10) : '')
    return stamp ? `历史报价 ${stamp}` : '历史报价（日期未知）'
  }, [quote.quoteDate, quote.quoteTime])

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
            成本价 <span className="tabular-nums">{safeFixed(costVal, 2)}</span> 元。
            防守线是**你**定的绝对价，跌到即提示执行纪律；不设则不告警。
          </div>
          <div className="flex items-center gap-2">
            <Input
              type="number"
              step="0.01"
              inputMode="decimal"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={`如 ${safeFixed(presetPrice(costVal, -8), 2)}`}
              className="tabular-nums"
              data-testid="defense-line-input"
            />
            <span className="shrink-0 text-muted-foreground">元</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {PRESETS.map((p) => (
              <Button
                key={p.pct}
                type="button"
                size="sm"
                variant="outline"
                className="h-7 px-2 text-[12px] tabular-nums"
                onClick={() => setDraft(safeFixed(presetPrice(costVal, p.pct), 2, ''))}
              >
                {p.label} · {safeFixed(presetPrice(costVal, p.pct), 2)}
              </Button>
            ))}
          </div>
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
  if (position.status === 'loading' || quote.status === 'loading') return null

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
          {lines.map((line) => (
            <div
              key={line.accountId || line.positionId}
              className={lineShell}
              data-testid={`defense-line-account-${line.accountId}`}
            >
              {multi && <AccountTag name={line.accountName} />}
              <ShieldPlus className="h-4 w-4 shrink-0 text-role-system" />
              <span className="font-medium text-foreground">未设置防守线</span>
              <span>
                成本 <span className="tabular-nums">{safeFixed(costVal, 2)}</span>，
                参考 -8% = <span className="tabular-nums">{safeFixed(presetPrice(costVal, -8), 2)}</span>
                <span className="ml-1 text-[12px] opacity-70">(系统参考值，非你的纪律位)</span>
              </span>
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="ml-auto h-7 shrink-0 px-2 text-[12px]"
                onClick={() => openDialog(line, presetPrice(costVal, -8))}
                data-testid={`defense-line-set-${line.accountId}`}
              >
                设置
              </Button>
            </div>
          ))}
        </div>
        {dialog}
      </>
    )
  }

  // ③ 设了防线但取不到现价: 显式降级, 不静默消失。
  if (quote.status === 'error' || quote.price == null) {
    return (
      <>
        <div
          className={`${shell} border-border bg-s1 text-role-watch ${multi ? 'flex-col items-stretch' : ''}`}
          data-testid="defense-line-degraded"
        >
          {lines.map((line) => (
            <div
              key={line.accountId || line.positionId}
              className={lineShell}
              data-testid={`defense-line-account-${line.accountId}`}
            >
              {multi && <AccountTag name={line.accountName} />}
              <ShieldAlert className="h-4 w-4 shrink-0" />
              <span>防守线无法计算（缺现价）</span>
              <span className="text-muted-foreground">
                防线{' '}
                <span className="tabular-nums">
                  {line.defensePrice === null ? '未设' : safeFixed(line.defensePrice, 2)}
                </span>
                ，成本 <span className="tabular-nums">{safeFixed(costVal, 2)}</span>
              </span>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="ml-auto h-7 shrink-0 px-2 text-[12px] text-muted-foreground"
                onClick={() => openDialog(line, line.defensePrice)}
              >
                修改
              </Button>
            </div>
          ))}
        </div>
        {dialog}
      </>
    )
  }

  const price = quote.price
  const lossPct = ((price - costVal) / costVal) * 100

  /** 单条防线：用共享判定（含行情新鲜度闸门）。 */
  const renderLine = (line: HeldLine) => {
    const { state, distancePct } = resolveDefenseState({
      price,
      defense: line.defensePrice,
      period: quote.period,
    })
    const breached = state === 'breached'
    const near = state === 'near'
    const stale = state === 'stale'
    const unset = state === 'unset'
    return {
      state,
      breached,
      near,
      stale,
      unset,
      distancePct,
      content: (
        <div
          key={line.accountId || line.positionId}
          className={lineShell}
          data-testid={`defense-line-account-${line.accountId}`}
          data-state={state}
        >
          {multi && <AccountTag name={line.accountName} />}
          {unset ? (
            <ShieldPlus className="h-4 w-4 shrink-0 text-role-system" />
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
              : breached
                ? '已击穿你的防守线'
                : stale
                  ? '行情滞后 · 未判破位'
                  : near
                    ? `接近你的防守线（${safeFixed(distancePct, 2)}%）`
                    : '防守线'}
          </span>
          <span className="tabular-nums">
            {line.defensePrice === null ? '未设' : `防线 ${safeFixed(line.defensePrice, 2)}`} · 现价{' '}
            {safeFixed(price, 2)}
            {lossPct != null ? ` · 浮亏 ${safeFixed(lossPct, 2)}%` : ''}
            {!breached && distancePct != null && !stale ? ` · 距防线 ${safeFixed(distancePct, 2)}%` : ''}
            {stale ? ` · ${staleLabel}${quote.quoteTime ? ` ${quote.quoteTime.slice(11, 16)}` : ''}` : ''}
          </span>
          <span className="text-[12px] opacity-70">
            成本 <span className="tabular-nums">{safeFixed(costVal, 2)}</span>
          </span>
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
  const toneOf = (state: DefenseState): string =>
    state === 'breached'
      ? 'border-rose-500/60 bg-rose-950/25 text-rose-100'
      : state === 'near'
        ? 'border-amber-500/50 bg-amber-950/20 text-amber-200'
        : state === 'stale'
          ? 'border-border bg-s1 text-role-watch'
          : 'border-border bg-s1 text-muted-foreground'

  // ④ 单账户且报价不是当日: 单独一档, 明说"未判破位"。
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
    return (
      <>
        <div
          className={`${shell} flex-col items-stretch ${multiTone}`}
          data-testid="defense-line-multi"
        >
          <div className="text-[12px] opacity-80">
            {lines.length} 个账户持有 {symbol}
            {new Set(lines.map((l) => l.defensePrice)).size > 1
              ? '，各自的防线不同（分行显示，不合并）'
              : '，防线一致'}
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
