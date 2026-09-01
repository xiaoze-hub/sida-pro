import { lazy, Suspense, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { CandlestickChart, Flag, LineChart, Search, Wallet, Loader2 } from 'lucide-react'

import InteractiveKline from '@panwatch/biz-ui/components/InteractiveKline'
import { insightApi } from '@panwatch/api'
import PageTabs, { type PageTabItem } from '@/components/PageTabs'
import { useSourceHealth } from '@/hooks/useSourceHealth'
import ContextCard, { isContextSource } from '@/components/ContextCard'

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
  label?: string
  // 各类事件的附加度量(后端按事件类型给出, 缺失即不显示, 不补 0)
  price?: number | null
  shares?: number | null
  amount?: number | null
  count?: number | null
  time?: string | null
}

/**
 * kind → 对应的 L4 事件图标(设计稿 §5.3)。
 * 解套盘位 / 支撑压力 按规范**缺位时不显示**(不是灰显), 故不在表里。
 */
const KIND_ICON: Record<string, string> = {
  dragon_tiger: '涨',
  split_cluster: '拆',
  cancel_anomaly: '⚠撤',
  support: '🛡托',
  pressure: '🔒压',
  my_trade: '我',
}

/** kind → 中文名(后端已给 label, 这里兜底用) */
const KIND_LABEL: Record<string, string> = {
  limit_up: '涨停',
  limit_down: '跌停',
  dragon_tiger: '龙虎榜',
  announcement: '公告',
  split_cluster: '拆单簇',
  cancel_anomaly: '撤单异常',
  support: '托盘',
  pressure: '压盘',
  unlock: '解套盘位',
  my_trade: '我的买卖点',
}

/** kind → 配色(红=利多/买入, 绿=利空/卖出, 其余中性) */
const KIND_TONE: Record<string, string> = {
  limit_up: 'text-rose-500',
  dragon_tiger: 'text-rose-500',
  split_cluster: 'text-rose-500',
  limit_down: 'text-emerald-500',
  cancel_anomaly: 'text-emerald-500',
  announcement: 'text-sky-500',
  my_trade: 'text-violet-500',
  unlock: 'text-amber-500',
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

/** 解套盘位价位线 —— 由后端标准筹码接口 chip_distribution 算出, 前端不再自算 */
interface UnlockLevel {
  price: number
  kind?: 'support' | 'pressure'
  label?: string
  ratio?: number | null
}

/** 标准筹码结构(chip_distribution.compute_near_term_chips 原样输出) */
interface ChipsInfo {
  peak_price?: number | null
  peak_ratio?: number | null
  cost_10?: number | null
  cost_50?: number | null
  cost_90?: number | null
  profit_ratio?: number | null
  concentration?: number | null
  cost_band?: { low?: number | null; high?: number | null; ratio?: number | null } | null
  last_close?: number | null
  source?: string | null
  window_days?: number | null
}

interface SummaryResp {
  symbol?: string
  market?: string
  fund_flow?: FundFlowRow[]
  events?: KlineEventItem[]
  orderbook?: OrderbookInfo | null
  main_intent?: string | null
  unlock_levels?: UnlockLevel[] | null
  chips?: ChipsInfo | null
}

/** 后端 events → InteractiveKline 的 KlineEvent(只保留组件认得的 kind) */
const VALID_KINDS = new Set([
  'limit_up', 'limit_down', 'dragon_tiger', 'announcement',
  'split_cluster', 'cancel_anomaly', 'support', 'pressure', 'unlock', 'my_trade',
])

function toKlineEvents(events?: KlineEventItem[]) {
  if (!events || events.length === 0) return undefined
  const out = events
    .filter((e) => e.date && VALID_KINDS.has(e.kind))
    .map((e) => ({
      date: fmtDate(e.date),
      kind: e.kind as never, // 已用 VALID_KINDS 收窄
      label: e.label || KIND_LABEL[e.kind] || e.kind,
    }))
  return out.length > 0 ? out : undefined
}

/** 后端解套盘位 → InteractiveKline 的支撑压力线 */
function toSupportPressure(levels?: UnlockLevel[] | null) {
  if (!levels || levels.length === 0) return undefined
  return levels
    .filter((l) => typeof l.price === 'number' && Number.isFinite(l.price))
    .map((l) => ({
      price: l.price,
      kind: (l.kind === 'support' ? 'support' : 'pressure') as 'support' | 'pressure',
      label: l.label,
    }))
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
  // §13: 持仓/自选/机会跳转来源 → 资金面板顶部显示对应上下文卡
  const sourceParam = params.get('source')
  const contextSource = isContextSource(sourceParam) ? sourceParam : null

  const [input, setInput] = useState(symbol)
  const [summary, setSummary] = useState<SummaryResp | null>(null)
  const [summaryLoading, setSummaryLoading] = useState(false)
  const [summaryError, setSummaryError] = useState('')
  // §12: 数据源健康状态(60s 轮询) → 决定 L4 事件图标灰显
  const { isReady, reasonOf } = useSourceHealth()

  useEffect(() => { setInput(symbol) }, [symbol])

  // summary 一次拉取供三个 Tab 复用: 资金(fund_flow) / 事件(events) /
  // 分时日K(L4 事件标注 + 解套盘位)。接口有 5 分钟缓存, 切 Tab 不重复请求。
  useEffect(() => {
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
  }, [symbol, type])

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
              // L4 事件标注 + 解套盘位(套牢区) 叠加到 K 线
              events={toKlineEvents(summary?.events)}
              supportPressure={toSupportPressure(summary?.unlock_levels)}
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
          <div className="space-y-3">
            {/* §13 持仓上下文卡: 按 ?source= 显示, 搜索进入(无 source)不显示 */}
            {contextSource && (
              <ContextCard source={contextSource} symbol={symbol} market="CN" />
            )}
            <FundPanel loading={summaryLoading} error={summaryError} rows={summary?.fund_flow} intent={summary?.main_intent} />
          </div>
        )}

        {activeTab === 'events' && (
          <EventsPanel
            loading={summaryLoading}
            error={summaryError}
            events={summary?.events}
            orderbook={summary?.orderbook}
            unlockLevels={summary?.unlock_levels}
            chips={summary?.chips}
            isReady={isReady}
            reasonOf={reasonOf}
          />
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

/**
 * 筹码结构 + 解套盘位。
 *
 * ⚠️ 2026-09-01 修正: 初版前端按"历史成交量分价位累加"自算套牢区, 那是近似值;
 * 现改为直接消费后端标准筹码接口 chip_distribution(腾讯当日分价表优先 / 新浪历史分价兜底),
 * 取不到就显式"无数据", 不做任何估算。
 */
function ChipsPanel({ chips, unlockLevels }: { chips?: ChipsInfo | null; unlockLevels?: UnlockLevel[] | null }) {
  if (!chips) {
    return (
      <div className="card overflow-hidden">
        <div className="border-b border-border/60 px-3 py-2 text-[12px] font-medium text-foreground">
          筹码结构 / 解套盘位
        </div>
        <div className="p-6 text-center text-[12px] text-muted-foreground">
          暂无筹码数据(标准筹码接口未取到, 不做估算)
        </div>
      </div>
    )
  }

  const pct = (v: number | null | undefined) =>
    typeof v === 'number' ? `${(v * 100).toFixed(1)}%` : '--'

  return (
    <div className="card overflow-hidden">
      <div className="flex items-center gap-2 border-b border-border/60 px-3 py-2">
        <span className="text-[12px] font-medium text-foreground">筹码结构 / 解套盘位</span>
        {chips.source && (
          <span className="ml-auto rounded-md bg-accent/40 px-1.5 py-0.5 text-[10px] text-muted-foreground">
            {chips.source}
          </span>
        )}
      </div>

      <div className="grid grid-cols-2 gap-x-6 gap-y-2 px-3 py-3 text-[12px] sm:grid-cols-4">
        <Field label="筹码峰" value={chips.peak_price ?? '--'} />
        <Field label="峰占比" value={chips.peak_ratio === null || chips.peak_ratio === undefined ? '--' : `${chips.peak_ratio.toFixed(1)}%`} />
        <Field label="获利盘" value={pct(chips.profit_ratio)} />
        <Field label="集中度" value={chips.concentration ?? '--'} />
        <Field label="成本10%" value={chips.cost_10 ?? '--'} />
        <Field label="成本50%" value={chips.cost_50 ?? '--'} />
        <Field label="成本90%" value={chips.cost_90 ?? '--'} />
        <Field label="现价" value={chips.last_close ?? '--'} />
      </div>

      {unlockLevels && unlockLevels.length > 0 ? (
        <ul className="divide-y divide-border/40 border-t border-border/50">
          {unlockLevels.map((l, i) => (
            <li key={`${l.price}-${i}`} className="flex items-center gap-3 px-3 py-2 text-[12px]">
              <span className={`font-mono ${l.kind === 'support' ? 'text-emerald-500' : 'text-amber-500'}`}>
                {l.price}
              </span>
              <span className="text-[11px] text-muted-foreground">{l.label || ''}</span>
              {typeof l.ratio === 'number' && (
                <span className="ml-auto font-mono text-[11px] text-muted-foreground">
                  占比 {l.ratio.toFixed(1)}%
                </span>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <div className="border-t border-border/50 p-4 text-center text-[12px] text-muted-foreground">
          暂无解套盘位价位线
        </div>
      )}

      <div className="border-t border-border/50 px-3 py-1.5 text-[11px] text-muted-foreground">
        数据源: 标准筹码接口(腾讯当日分价表优先 / 新浪历史分价兜底), 窗口
        {chips.window_days ?? 10} 个交易日 · 绿色支撑 / 琥珀色压力
      </div>
    </div>
  )
}

/** 事件明细: 按事件类型给出度量, 缺字段就不显示(不补 0) */
function EventMetrics({ e }: { e: KlineEventItem }) {
  const parts: string[] = []
  if (e.time) parts.push(e.time)
  if (typeof e.price === 'number') parts.push(`${e.price}`)
  if (typeof e.shares === 'number') parts.push(`${e.shares.toLocaleString('zh-CN')} 股`)
  if (typeof e.count === 'number') parts.push(`${e.count} 笔`)
  if (typeof e.amount === 'number') parts.push(`${toWan(e.amount)} 万元`)
  if (parts.length === 0) return null
  return <span className="font-mono text-[11px] text-muted-foreground">{parts.join(' · ')}</span>
}

/** 事件 Tab: L4 事件标注 + 盘口托压单 + 解套盘位 */
function EventsPanel({
  loading, error, events, orderbook, unlockLevels, chips, isReady, reasonOf,
}: {
  loading: boolean
  error: string
  events?: KlineEventItem[]
  orderbook?: OrderbookInfo | null
  unlockLevels?: UnlockLevel[] | null
  chips?: ChipsInfo | null
  /** §12: 图标是否可用(数据源健康) */
  isReady: (icon: string) => boolean
  /** §12: 灰显 tooltip 文案 */
  reasonOf: (icon: string) => string
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

      <ChipsPanel chips={chips} unlockLevels={unlockLevels} />

      <div className="card overflow-hidden">
        <div className="border-b border-border/60 px-3 py-2 text-[12px] font-medium text-foreground">
          事件标注(L4)
        </div>
        {events && events.length > 0 ? (
          <ul className="divide-y divide-border/40">
            {events.slice().reverse().map((e, i) => {
              const icon = KIND_ICON[e.kind]
              // §12 缺位兜底: 图标对应数据源不可用 → 灰显 + tooltip
              const ready = icon ? isReady(icon) : true
              const why = icon ? reasonOf(icon) : ''
              return (
                <li key={`${e.date}-${e.kind}-${i}`} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-[12px]">
                  <span className="font-mono text-[11px] text-muted-foreground">{fmtDate(e.date)}</span>
                  {icon && (
                    <span
                      className={`inline-flex h-5 min-w-5 items-center justify-center rounded px-1 text-[11px] ${
                        ready ? 'bg-accent/50 text-foreground' : 'bg-muted text-muted-foreground/50 grayscale'
                      }`}
                      title={ready ? undefined : why || '数据源不可用'}
                    >
                      {icon}
                    </span>
                  )}
                  <span className={`text-[12px] font-medium ${ready ? (KIND_TONE[e.kind] || 'text-foreground') : 'text-muted-foreground/60'}`}>
                    {e.label || KIND_LABEL[e.kind] || e.kind}
                  </span>
                  <EventMetrics e={e} />
                </li>
              )
            })}
          </ul>
        ) : (
          <div className="p-6 text-center text-[12px] text-muted-foreground">
            暂无事件标注(拆单簇 / 撤单异常需 .tck 文件, 龙虎榜 / 公告需 wencai, 我的买卖点需交割单;
            数据源缺失时不编造)
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
