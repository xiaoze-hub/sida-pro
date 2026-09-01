import { lazy, Suspense, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { CandlestickChart, Flag, LineChart, Search, Wallet, Loader2 } from 'lucide-react'

import InteractiveKline from '@panwatch/biz-ui/components/InteractiveKline'
import { insightApi } from '@panwatch/api'
import PageTabs, { type PageTabItem } from '@/components/PageTabs'

/**
 * 行情三合一(设计稿 §4.3): 个股 + 指数 + 板块 合成行情页, /forecast 作为入口。
 *
 * 内部 4 个 Tab: 分时/日K | 预测 | 资金 | 事件。
 * Tab 与标的都写进 URL(?tab= / ?symbol= / ?type=), 可分享可刷新可后退。
 *
 * 数据:
 *   - 分时/日K → InteractiveKline(组件内含 分时/日周月 切换 + L1~L4 图层)
 *   - 预测     → 复用既有 ForecastPage(懒加载)
 *   - 资金     → /klines/{symbol}/summary 的 fund_flow / main_intent
 *   - 事件     → /klines/{symbol}/summary 的 events + orderbook(托压单)
 *
 * 缺失一律显式"无数据", 不编造(板块 K 线后端未提供, 明确引导去板块详情页)。
 */
const ForecastPage = lazy(() => import('@/pages/Forecast'))

type QuoteType = 'stock' | 'index' | 'board'

const TYPE_OPTIONS: Array<{ key: QuoteType; label: string }> = [
  { key: 'stock', label: '个股' },
  { key: 'index', label: '指数' },
  { key: 'board', label: '板块' },
]

/** 常用指数快捷入口(与后端 MARKET_INDICES 保持一致) */
const QUICK_INDICES = [
  { symbol: '000001', name: '上证指数' },
  { symbol: '399001', name: '深证成指' },
  { symbol: '399006', name: '创业板指' },
]

const QUOTE_TABS: PageTabItem[] = [
  { key: 'chart', label: '分时/日K', icon: CandlestickChart },
  { key: 'forecast', label: '预测', icon: LineChart },
  { key: 'fund', label: '资金', icon: Wallet },
  { key: 'events', label: '事件', icon: Flag },
]

interface FundFlowRow {
  date: string
  ming_net: number | null
  dark_net: number | null
}

interface KlineEventItem {
  date: string
  kind: string
  label: string
}

interface OrderbookInfo {
  available?: boolean
  shape?: string | null
  best_bid?: number | null
  best_ask?: number | null
  spread?: number | null
  bid_pressure?: number | null
  queue_shares?: number | null
  note?: string | null
}

interface SummaryResp {
  symbol?: string
  market?: string
  fund_flow?: FundFlowRow[]
  events?: KlineEventItem[]
  orderbook?: OrderbookInfo | null
  main_intent?: string | null
}

/** 元 → 万元; 空值一律 '--'(不把 null 当 0) */
function toWan(v: number | null | undefined): string {
  if (v === null || v === undefined) return '--'
  const wan = v / 10000
  const sign = wan > 0 ? '+' : ''
  return `${sign}${wan.toFixed(2)}`
}

function fmtDate(d: string): string {
  const s = String(d || '').trim()
  if (s.length === 8 && /^\d{8}$/.test(s)) return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6)}`
  return s.slice(0, 10) || '--'
}

export default function QuotePage() {
  const [params, setParams] = useSearchParams()
  const symbol = (params.get('symbol') || '000001').trim()
  const type = (params.get('type') as QuoteType) || 'stock'
  const tab = params.get('tab') || 'chart'

  const [input, setInput] = useState(symbol)
  const [summary, setSummary] = useState<SummaryResp | null>(null)
  const [summaryLoading, setSummaryLoading] = useState(false)
  const [summaryError, setSummaryError] = useState('')

  useEffect(() => { setInput(symbol) }, [symbol])

  // 资金 / 事件两个 Tab 共用同一份 summary(接口 5 分钟缓存, 不必各拉一次)
  useEffect(() => {
    if (tab !== 'fund' && tab !== 'events') return
    if (!symbol || type === 'board') return
    let alive = true
    setSummaryLoading(true)
    setSummaryError('')
    insightApi
      .klineSummary<SummaryResp>(symbol, 'CN')
      .then((d) => { if (alive) setSummary(d ?? null) })
      .catch((e: unknown) => {
        if (alive) setSummaryError(e instanceof Error ? e.message : '加载失败')
      })
      .finally(() => { if (alive) setSummaryLoading(false) })
    return () => { alive = false }
  }, [symbol, type, tab])

  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    setParams(next)
  }

  const submitSymbol = () => {
    const v = input.trim()
    if (!v) return
    setParam('symbol', v)
  }

  const activeTab = QUOTE_TABS.some((t) => t.key === tab) ? tab : 'chart'

  return (
    <div className="w-full space-y-4">
      {/* 页头 + 标的选择(个股/指数/板块 三合一) */}
      <div className="card p-4">
        <div className="flex items-start justify-between gap-3 flex-wrap">
          <div>
            <h1 className="text-lg font-bold text-foreground">行情</h1>
            <p className="mt-0.5 text-[12px] text-muted-foreground">
              个股 / 指数 / 板块 合成行情页 — 分时日K、预测、资金、事件
            </p>
          </div>
          <span className="rounded-md bg-accent/40 px-2 py-1 font-mono text-[11px] text-muted-foreground">
            {symbol}
            <span className="ml-1.5 opacity-70">
              {TYPE_OPTIONS.find((t) => t.key === type)?.label ?? type}
            </span>
          </span>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <div className="inline-flex items-center gap-1 rounded-xl border border-border/50 bg-accent/40 p-1">
            {TYPE_OPTIONS.map((t) => (
              <button
                key={t.key}
                type="button"
                onClick={() => setParam('type', t.key)}
                className={`rounded-lg px-2.5 py-1 text-[12px] font-medium transition-colors ${
                  type === t.key
                    ? 'bg-primary text-primary-foreground shadow-sm'
                    : 'text-muted-foreground hover:bg-accent hover:text-foreground'
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>

          <div className="flex min-w-[220px] flex-1 items-center gap-1.5">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') submitSymbol() }}
              placeholder={type === 'board' ? '板块代码' : '代码, 如 000977'}
              className="h-8 min-w-0 flex-1 rounded-lg border border-border/50 bg-background px-2.5 text-[12px] text-foreground outline-none focus:border-primary/50"
            />
            <button
              type="button"
              onClick={submitSymbol}
              className="inline-flex h-8 shrink-0 items-center gap-1 rounded-lg border border-border/50 bg-background px-2.5 text-[12px] text-muted-foreground transition-colors hover:text-foreground"
            >
              <Search className="h-3.5 w-3.5" />
              查询
            </button>
          </div>
        </div>

        {type === 'index' && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <span className="text-[11px] text-muted-foreground">常用指数</span>
            {QUICK_INDICES.map((i) => (
              <button
                key={i.symbol}
                type="button"
                onClick={() => setParam('symbol', i.symbol)}
                className={`rounded-lg border px-2 py-0.5 text-[11px] transition-colors ${
                  symbol === i.symbol
                    ? 'border-primary/40 bg-primary/10 text-foreground'
                    : 'border-border/50 text-muted-foreground hover:text-foreground'
                }`}
              >
                {i.name}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* 4 个 Tab */}
      <PageTabs
        tabs={QUOTE_TABS}
        value={activeTab}
        onChange={(k) => setParam('tab', k)}
      />

      <div className="min-w-0">
        {activeTab === 'chart' && (
          type === 'board' ? (
            <div className="card p-8 text-center">
              <div className="text-[13px] font-medium text-foreground">板块 K 线待接入</div>
              <p className="mt-1 text-[12px] text-muted-foreground">
                板块行情数据在板块详情页, 请前往 /boards/{symbol} 查看(不编造图表)
              </p>
            </div>
          ) : (
            <InteractiveKline
              key={`${type}:${symbol}`}
              symbol={symbol}
              market="CN"
              initialInterval="1d"
              initialDays="120"
            />
          )
        )}

        {activeTab === 'forecast' && (
          <Suspense fallback={
            <div className="flex h-[40vh] items-center justify-center text-[12px] text-muted-foreground">
              <Loader2 className="mr-2 h-4 w-4 animate-spin" /> 加载预测页…
            </div>
          }>
            <ForecastPage />
          </Suspense>
        )}

        {activeTab === 'fund' && (
          <FundPanel loading={summaryLoading} error={summaryError} rows={summary?.fund_flow} intent={summary?.main_intent} />
        )}

        {activeTab === 'events' && (
          <EventsPanel loading={summaryLoading} error={summaryError} events={summary?.events} orderbook={summary?.orderbook} />
        )}
      </div>
    </div>
  )
}

/** 资金 Tab: 明盘/暗盘净额(万元) + 主力意图 */
function FundPanel({
  loading, error, rows, intent,
}: {
  loading: boolean
  error: string
  rows?: FundFlowRow[]
  intent?: string | null
}) {
  if (loading) return <PanelLoading text="加载资金流…" />
  if (error) return <PanelError text={error} />
  if (!rows || rows.length === 0) return <PanelEmpty text="暂无资金流数据(接口无数据, 不编造)" />

  const recent = rows.slice(-20).reverse()
  return (
    <div className="space-y-3">
      {intent && (
        <div className="card p-4">
          <div className="text-[12px] font-medium text-foreground">主力意图</div>
          <p className="mt-1.5 whitespace-pre-wrap text-[12px] leading-relaxed text-muted-foreground">{intent}</p>
        </div>
      )}
      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-[12px]">
            <thead>
              <tr className="border-b border-border/60 bg-accent/20 text-[11px] text-muted-foreground">
                <th className="px-3 py-2 font-medium">日期</th>
                <th className="px-3 py-2 text-right font-medium">明盘净额(万)</th>
                <th className="px-3 py-2 text-right font-medium">暗盘净额(万)</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border/40">
              {recent.map((r) => (
                <tr key={r.date} className="hover:bg-accent/20">
                  <td className="px-3 py-2 font-mono text-muted-foreground">{fmtDate(r.date)}</td>
                  <td className={`px-3 py-2 text-right font-mono ${(r.ming_net ?? 0) > 0 ? 'text-rose-500' : (r.ming_net ?? 0) < 0 ? 'text-emerald-500' : 'text-muted-foreground'}`}>
                    {toWan(r.ming_net)}
                  </td>
                  <td className={`px-3 py-2 text-right font-mono ${(r.dark_net ?? 0) > 0 ? 'text-rose-500' : (r.dark_net ?? 0) < 0 ? 'text-emerald-500' : 'text-muted-foreground'}`}>
                    {toWan(r.dark_net)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="border-t border-border/50 px-3 py-1.5 text-[11px] text-muted-foreground">
          单位: 万元(接口原始单位为元) · 红色净流入 / 绿色净流出 · 仅显示最近 {recent.length} 个交易日
        </div>
      </div>
    </div>
  )
}

/** 事件 Tab: L4 事件标注 + 盘口托压单 */
function EventsPanel({
  loading, error, events, orderbook,
}: {
  loading: boolean
  error: string
  events?: KlineEventItem[]
  orderbook?: OrderbookInfo | null
}) {
  if (loading) return <PanelLoading text="加载事件…" />
  if (error) return <PanelError text={error} />

  return (
    <div className="space-y-3">
      <div className="card p-4">
        <div className="text-[12px] font-medium text-foreground">盘口 / 托压单</div>
        {orderbook?.available ? (
          <div className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1.5 text-[12px] sm:grid-cols-4">
            <Field label="形态" value={orderbook.shape ?? '--'} />
            <Field label="买一" value={orderbook.best_bid ?? '--'} />
            <Field label="卖一" value={orderbook.best_ask ?? '--'} />
            <Field label="买盘占比" value={orderbook.bid_pressure === null || orderbook.bid_pressure === undefined ? '--' : `${(orderbook.bid_pressure * 100).toFixed(1)}%`} />
          </div>
        ) : (
          <p className="mt-1.5 text-[12px] text-muted-foreground">
            {orderbook?.note || '暂无盘口数据(需 .img 数据源或 thsdk 实时快照, 不编造)'}
          </p>
        )}
      </div>

      <div className="card overflow-hidden">
        <div className="border-b border-border/60 px-3 py-2 text-[12px] font-medium text-foreground">
          事件标注(L4)
        </div>
        {events && events.length > 0 ? (
          <ul className="divide-y divide-border/40">
            {events.slice().reverse().map((e, i) => (
              <li key={`${e.date}-${e.kind}-${i}`} className="flex items-center gap-3 px-3 py-2 text-[12px]">
                <span className="font-mono text-[11px] text-muted-foreground">{fmtDate(e.date)}</span>
                <span className="rounded-md bg-accent/50 px-1.5 py-0.5 text-[11px] text-foreground">{e.label}</span>
                <span className="text-[11px] text-muted-foreground">{e.kind}</span>
              </li>
            ))}
          </ul>
        ) : (
          <div className="p-6 text-center text-[12px] text-muted-foreground">
            暂无事件标注(拆单簇 / 撤单 / 龙虎榜 / 公告 待第 4 块接入, 不编造)
          </div>
        )}
      </div>
    </div>
  )
}

function Field({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="truncate font-mono text-foreground">{value}</div>
    </div>
  )
}

function PanelLoading({ text }: { text: string }) {
  return (
    <div className="card flex h-[30vh] items-center justify-center text-[12px] text-muted-foreground">
      <Loader2 className="mr-2 h-4 w-4 animate-spin" /> {text}
    </div>
  )
}

function PanelError({ text }: { text: string }) {
  return <div className="card p-6 text-center text-[12px] text-rose-500">{text}</div>
}

function PanelEmpty({ text }: { text: string }) {
  return <div className="card p-6 text-center text-[12px] text-muted-foreground">{text}</div>
}
