import { useCallback, useEffect, useMemo, useState } from 'react'
import { BellRing, ListPlus, Loader2, Minus, Plus, Sparkles, Wallet } from 'lucide-react'

import { fetchAPI, recommendationsApi, stocksApi } from '@panwatch/api'

/**
 * 设计稿 v2.1 §13: 持仓详情跳路由决策。
 *
 * 行情页资金面板顶部按 `?source=` 显示三种上下文卡:
 *   holdings      → 成本价/现价/盈亏%/股数 + 加仓/减仓
 *   watchlist     → 自选状态 + 价格提醒设置入口
 *   opportunities → 入选时间/触发指标/入场计划 + 加入自选
 *
 * ⚠️ 与设计稿的 4 处现实偏差(诚实口径, 不编造):
 *   1. 持仓 API 不返回建仓时间 → 持仓天数显示 '--'(数据缺失, 不算 0 天)
 *   2. 自选无"分组/标签"字段(数据库层就没有) → 该行不渲染
 *   3. 机会卡"历史命中率"无单股粒度字段 → 用 score/confidence 呈现, 不冒充命中率
 *   4. 搜索跳转无 source 参数 → 不显示任何卡(设计稿默认行为)
 */

export type ContextSource = 'holdings' | 'watchlist' | 'opportunities'

export function isContextSource(v: string | null | undefined): v is ContextSource {
  return v === 'holdings' || v === 'watchlist' || v === 'opportunities'
}

// ---------------------------------------------------------------------------
// 数据形状(只声明实际消费的字段)
// ---------------------------------------------------------------------------

interface PositionRow {
  id: number
  symbol: string
  market?: string
  cost_price: number
  quantity: number
  current_price?: number | null
  pnl?: number | null
  pnl_pct?: number | null
  trading_style?: string | null
}

interface PortfolioSummaryResp {
  accounts?: Array<{ positions?: PositionRow[] }>
}

interface StockItem {
  id: number
  symbol: string
  name?: string
  market?: string
}

interface AlertRow {
  stock_symbol: string
  name?: string
  enabled?: boolean
}

interface EntryCandidate {
  id: number
  stock_symbol: string
  stock_market?: string
  stock_name?: string
  snapshot_date?: string
  score?: number
  confidence?: number | null
  action?: string
  action_label?: string
  reason?: string
  entry_low?: number | null
  entry_high?: number | null
  stop_loss?: number | null
  target_price?: number | null
  strategy_labels?: string[]
  created_at?: string
}

const fmt = (v: number | null | undefined, suffix = ''): string =>
  typeof v === 'number' && Number.isFinite(v) ? `${v.toFixed(2)}${suffix}` : '--'

// ---------------------------------------------------------------------------
// 主组件: 按 source 分发
// ---------------------------------------------------------------------------

export default function ContextCard({
  source, symbol, market,
}: {
  source: ContextSource
  symbol: string
  market: string
}) {
  if (!symbol) return null

  if (source === 'holdings') return <HoldingsCard symbol={symbol} market={market} />
  if (source === 'watchlist') return <WatchlistCard symbol={symbol} market={market} />
  return <OpportunitiesCard symbol={symbol} market={market} />
}

function CardShell({ icon: Icon, title, children }: {
  icon: typeof Wallet
  title: string
  children: React.ReactNode
}) {
  return (
    <div className="border-b border-border/40 pb-3">
      <div className="mb-2 flex items-center gap-2">
        <div className="flex h-6 w-6 items-center justify-center rounded-lg bg-accent/40 text-primary ring-1 ring-border/40">
          <Icon className="h-3.5 w-3.5" />
        </div>
        <span className="text-[12px] font-semibold text-foreground">{title}</span>
      </div>
      {children}
    </div>
  )
}

function CardLoading({ text }: { text: string }) {
  return (
    <div className="flex items-center gap-2 py-3 text-[12px] text-muted-foreground">
      <Loader2 className="h-3.5 w-3.5 animate-spin" /> {text}
    </div>
  )
}

function CardEmpty({ text }: { text: string }) {
  return <p className="py-2 text-[12px] text-muted-foreground">{text}</p>
}

// ---------------------------------------------------------------------------
// holdings 卡
// ---------------------------------------------------------------------------

function HoldingsCard({ symbol }: { symbol: string; market: string }) {
  const [loading, setLoading] = useState(true)
  const [pos, setPos] = useState<PositionRow | null>(null)
  const [notFound, setNotFound] = useState(false)
  // 加仓/减仓: 展开行内输入, 确认后调 PUT /positions/{id}
  const [editing, setEditing] = useState<'add' | 'reduce' | null>(null)
  const [qty, setQty] = useState('')
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await fetchAPI<PortfolioSummaryResp>('/portfolio/summary', { timeoutMs: 30000 })
      const rows = (data?.accounts || []).flatMap(a => a?.positions || [])
      // market 兼容: 接口 market 可能是 SH/SZ(交易所) 或 CN, 匹配以 symbol 为主
      const hit = rows.find(r => r.symbol === symbol) || null
      setPos(hit)
      setNotFound(!hit)
    } catch {
      setNotFound(true)
    } finally {
      setLoading(false)
    }
  }, [symbol])

  useEffect(() => { void load() }, [load])

  const submitQty = async (mode: 'add' | 'reduce') => {
    if (!pos) return
    const delta = parseInt(qty, 10)
    if (!Number.isFinite(delta) || delta <= 0) {
      setMsg('请输入正整数数量')
      return
    }
    const nextQty = mode === 'add' ? pos.quantity + delta : pos.quantity - delta
    if (nextQty < 0) {
      setMsg('减仓数量超过持仓')
      return
    }
    setSaving(true)
    setMsg('')
    try {
      await fetchAPI(`/positions/${pos.id}`, {
        method: 'PUT',
        body: JSON.stringify({ quantity: nextQty }),
      })
      setMsg(nextQty === 0 ? '已清仓' : `已${mode === 'add' ? '加仓' : '减仓'} ${delta} 股`)
      setEditing(null)
      setQty('')
      await load()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '操作失败')
    } finally {
      setSaving(false)
    }
  }

  if (loading) return <CardShell icon={Wallet} title="持仓上下文"><CardLoading text="加载持仓…" /></CardShell>

  return (
    <CardShell icon={Wallet} title="持仓上下文">
      {notFound || !pos ? (
        <CardEmpty text={`未持有 ${symbol}(按 symbol 匹配, 无匹配持仓)`} />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-[12px] sm:grid-cols-3">
            <Field label="成本价" value={fmt(pos.cost_price)} />
            <Field label="现价" value={fmt(pos.current_price)} />
            <Field
              label="盈亏"
              value={fmt(pos.pnl_pct, '%')}
              tone={(pos.pnl_pct ?? 0) > 0 ? 'up' : (pos.pnl_pct ?? 0) < 0 ? 'down' : undefined}
            />
            <Field label="持仓股数" value={String(pos.quantity)} />
            <Field label="持仓天数" value="--" hint="接口未返回建仓时间" />
            {pos.trading_style && <Field label="风格" value={pos.trading_style} />}
          </div>

          <div className="mt-2.5 flex items-center gap-2">
            {!editing && (
              <>
                <button type="button" onClick={() => { setEditing('add'); setMsg('') }}
                  className="inline-flex items-center gap-1 rounded-lg border border-border/50 px-2.5 py-1 text-[11px] text-stock-up hover:bg-accent">
                  <Plus className="h-3 w-3" /> 加仓
                </button>
                <button type="button" onClick={() => { setEditing('reduce'); setMsg('') }}
                  className="inline-flex items-center gap-1 rounded-lg border border-border/50 px-2.5 py-1 text-[11px] text-stock-down hover:bg-accent">
                  <Minus className="h-3 w-3" /> 减仓
                </button>
              </>
            )}
            {editing && (
              <div className="flex items-center gap-1.5">
                <input
                  autoFocus
                  value={qty}
                  onChange={e => setQty(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') void submitQty(editing) }}
                  placeholder="数量(股)"
                  className="h-7 w-24 rounded-md border border-border/50 bg-background px-2 text-[11px] outline-none focus:border-primary/50"
                />
                <button type="button" disabled={saving} onClick={() => void submitQty(editing)}
                  className="rounded-md bg-primary px-2 py-1 text-[11px] text-primary-foreground disabled:opacity-50">
                  {saving ? '保存中…' : '确认'}
                </button>
                <button type="button" onClick={() => { setEditing(null); setMsg('') }}
                  className="rounded-md px-2 py-1 text-[11px] text-muted-foreground hover:text-foreground">
                  取消
                </button>
              </div>
            )}
            {msg && <span className="text-[11px] text-muted-foreground">{msg}</span>}
          </div>
          <p className="mt-1.5 text-[10px] text-muted-foreground/70">
            加/减仓调 PUT /positions/{pos.id}(现有接口); 持仓天数接口未返回建仓时间, 如实显示 --
          </p>
        </>
      )}
    </CardShell>
  )
}

// ---------------------------------------------------------------------------
// watchlist 卡
// ---------------------------------------------------------------------------

function WatchlistCard({ symbol, market }: { symbol: string; market: string }) {
  const [loading, setLoading] = useState(true)
  const [stock, setStock] = useState<StockItem | null>(null)
  const [alerts, setAlerts] = useState<AlertRow[]>([])
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [stocks, alertResp] = await Promise.all([
        stocksApi.list(),
        fetchAPI<{ items?: AlertRow[] } | AlertRow[]>('/price-alerts').catch(() => null),
      ])
      setStock((stocks || []).find(s => s.symbol === symbol) || null)
      const raw = alertResp as { items?: AlertRow[] } | AlertRow[] | null
      setAlerts(Array.isArray(raw) ? raw : raw?.items || [])
    } catch {
      setStock(null)
    } finally {
      setLoading(false)
    }
  }, [symbol])

  useEffect(() => { void load() }, [load])

  const myAlerts = useMemo(
    () => alerts.filter(a => a.stock_symbol === symbol),
    [alerts, symbol],
  )

  const addWatch = async () => {
    setBusy(true)
    setMsg('')
    try {
      await stocksApi.create({ symbol, market, name: '' } as never)
      setMsg('已加入自选')
      await load()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '加入失败')
    } finally {
      setBusy(false)
    }
  }

  const removeWatch = async () => {
    if (!stock) return
    setBusy(true)
    setMsg('')
    try {
      await stocksApi.remove(stock.id)
      setMsg('已移出自选')
      setStock(null)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '移出失败')
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <CardShell icon={ListPlus} title="自选上下文"><CardLoading text="加载自选…" /></CardShell>

  return (
    <CardShell icon={ListPlus} title="自选上下文">
      <div className="text-[12px]">
        {stock ? (
          <div className="flex items-center gap-2">
            <span className="rounded-md bg-emerald-500/10 px-1.5 py-0.5 text-[11px] text-emerald-600">已在自选</span>
            <button type="button" disabled={busy} onClick={() => void removeWatch()}
              className="rounded-lg border border-border/50 px-2 py-0.5 text-[11px] text-muted-foreground hover:text-foreground disabled:opacity-50">
              移出自选
            </button>
          </div>
        ) : (
          <div className="flex items-center gap-2">
            <span className="text-muted-foreground">未在自选</span>
            <button type="button" disabled={busy} onClick={() => void addWatch()}
              className="inline-flex items-center gap-1 rounded-lg bg-primary px-2 py-0.5 text-[11px] text-primary-foreground disabled:opacity-50">
              <ListPlus className="h-3 w-3" /> 加入自选
            </button>
          </div>
        )}
        {msg && <span className="ml-2 text-[11px] text-muted-foreground">{msg}</span>}
      </div>

      <div className="mt-2.5 border-t border-border/40 pt-2">
        <div className="mb-1 flex items-center gap-1.5 text-[11px] font-medium text-foreground">
          <BellRing className="h-3 w-3 text-amber-500" /> 价格提醒
        </div>
        {myAlerts.length > 0 ? (
          <ul className="space-y-1">
            {myAlerts.map((a, i) => (
              <li key={i} className="flex items-center gap-2 text-[11px] text-muted-foreground">
                <span className={a.enabled === false ? 'opacity-50' : 'text-foreground'}>{a.name || `规则${i + 1}`}</span>
                {a.enabled === false && <span className="rounded bg-muted px-1 text-[10px]">已停用</span>}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[11px] text-muted-foreground">
            暂无提醒规则 —— 前往 提醒 页创建(/alerts)
          </p>
        )}
      </div>

      {/* ⚠️ 设计稿要求"加入自选时间/分组标签", 但接口不返回 created_at 且无分组字段 → 如实不渲染 */}
      <p className="mt-1.5 text-[10px] text-muted-foreground/70">
        加入时间与分组标签接口未提供, 不显示(不编造)
      </p>
    </CardShell>
  )
}

// ---------------------------------------------------------------------------
// opportunities 卡
// ---------------------------------------------------------------------------

function OpportunitiesCard({ symbol, market }: { symbol: string; market: string }) {
  const [loading, setLoading] = useState(true)
  const [cand, setCand] = useState<EntryCandidate | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    let alive = true
    setLoading(true)
    recommendationsApi
      .listEntryCandidates({ limit: 100 })
      .then((resp: unknown) => {
        if (!alive) return
        const rows = (resp as { items?: EntryCandidate[] })?.items || []
        setCand(rows.find(r => r.stock_symbol === symbol) || null)
      })
      .catch(() => { if (alive) setCand(null) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [symbol])

  const addWatch = async () => {
    setBusy(true)
    setMsg('')
    try {
      await stocksApi.create({ symbol, market, name: cand?.stock_name || '' } as never)
      setMsg('已加入自选')
    } catch (e) {
      setMsg(e instanceof Error ? e.message : '加入失败')
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <CardShell icon={Sparkles} title="机会上下文"><CardLoading text="加载机会…" /></CardShell>

  return (
    <CardShell icon={Sparkles} title="机会上下文">
      {!cand ? (
        <CardEmpty text={`${symbol} 不在近期机会候选中(按 symbol 匹配)`} />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-[12px] sm:grid-cols-3">
            <Field label="入选日期" value={cand.snapshot_date || '--'} />
            <Field label="评分" value={typeof cand.score === 'number' ? String(cand.score) : '--'} />
            <Field label="置信度" value={fmt(cand.confidence)} />
            <Field label="动作" value={cand.action_label || cand.action || '--'} />
            <Field label="入场区间" value={cand.entry_low != null && cand.entry_high != null ? `${cand.entry_low} ~ ${cand.entry_high}` : '--'} />
            <Field label="止损" value={fmt(cand.stop_loss)} />
            <Field label="目标价" value={fmt(cand.target_price)} />
          </div>

          {cand.reason && (
            <p className="mt-2 line-clamp-3 text-[11px] leading-relaxed text-muted-foreground" title={cand.reason}>
              {cand.reason}
            </p>
          )}

          <div className="mt-2.5 flex items-center gap-2">
            <button type="button" disabled={busy} onClick={() => void addWatch()}
              className="inline-flex items-center gap-1 rounded-lg bg-primary px-2.5 py-1 text-[11px] text-primary-foreground disabled:opacity-50">
              <ListPlus className="h-3 w-3" /> 加入自选
            </button>
            {msg && <span className="text-[11px] text-muted-foreground">{msg}</span>}
          </div>
          {/* ⚠️ 设计稿要求"历史命中率", 接口无单股粒度命中率字段 → 用 score/confidence 呈现, 不冒充 */}
          <p className="mt-1.5 text-[10px] text-muted-foreground/70">
            历史命中率接口无单股字段, 以评分/置信度呈现(不冒充命中率)
          </p>
        </>
      )}
    </CardShell>
  )
}

// ---------------------------------------------------------------------------
// 通用小件
// ---------------------------------------------------------------------------

function Field({ label, value, tone, hint }: {
  label: string
  value: string
  tone?: 'up' | 'down'
  hint?: string
}) {
  return (
    <div className="min-w-0" title={hint}>
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className={`truncate font-mono ${
        tone === 'up' ? 'text-stock-up' : tone === 'down' ? 'text-stock-down' : 'text-foreground'
      }`}>{value}</div>
    </div>
  )
}
