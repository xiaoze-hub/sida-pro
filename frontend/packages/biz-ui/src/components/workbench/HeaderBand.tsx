import { useCallback, useEffect, useMemo, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { fetchAPI, insightApi } from '@panwatch/api'
import { cn } from '@panwatch/base-ui'
import { TechnicalBadge, technicalToneFromSuggestionAction } from '@panwatch/biz-ui/components/technical-badge'
import { fmtAmount } from '@panwatch/biz-ui/lib/ladder-format'
import { buildKlineSuggestion } from '@/lib/kline-scorer'
import { safeFixed, safeNum, safePrice } from '@/lib/format'
import type { KlineSummaryData } from '@panwatch/biz-ui/components/kline-summary-dialog'
import type { WorkbenchTab, WorkbenchType } from '@/lib/workbench-tabs'

/**
 * 带1 顶部信息带(v0.5.96, 三合一 spec §4.1): 个股/指数/板块**共享**。
 *
 * 结构: 顶行(名称 + 代码 + 现价 + 涨跌色 + 类型三按钮 + 刷新)
 *       + 快照行(今开/最高/最低/成交额/换手率/量比/总市值/涨停价)
 *       + 技术指标买卖建议条(仅 type=stock; 点击跳「建议」标签)。
 *
 * 数据面(全部真数据, 缺值 `--`, 绝不编造):
 *  - GET /quotes/{symbol}            → current_price/change_pct/open_price/high_price/low_price/turnover
 *  - GET /quotes/{symbol}/more-info  → turnover_rate/volume_ratio/total_market_value(亿)
 *  - GET /stocks/{symbol}/l2         → more.zt_price(涨停价; 自包含, 不等兄弟组件回喂)
 *  - GET /klines/{symbol}/summary    → buildKlineSuggestion(技术面建议)
 * 任一接口失败**保留旧值**(stale-on-error), 不把失败渲染成 0/编造值。
 */

/** 快照行单元格: key 稳定(测试/去重表锚点), label 面向用户, value 一律已格式化字符串。 */
export interface SnapshotCell {
  key: string
  label: string
  value: string
}

/** GET /quotes/{symbol} 响应(字段名对齐 src/web/api/quotes.py::_quote_to_response)。 */
export interface QuoteSnapshot {
  name?: string | null
  /** 现价(元) */
  current_price?: number | string | null
  /** 涨跌幅(%) */
  change_pct?: number | string | null
  /** 今开(元) */
  open_price?: number | string | null
  /** 最高(元) */
  high_price?: number | string | null
  /** 最低(元) */
  low_price?: number | string | null
  /** 成交额(元); 单位证据见 packages/marketdata .../types.py::Quote.turnover */
  turnover?: number | string | null
}

/**
 * more-info 侧字段(packages/marketdata types.py::MoreInfo 透传) + `/stocks/{s}/l2` 注入的 zt_price。
 * 注意 total_market_value 单位是**亿**(Zsz 原值未换算), 展示必须带「亿」, 不可再过 fmtAmount(元口径)。
 */
export interface MoreInfoSnapshot {
  /** 换手率(%) — fHSL */
  turnover_rate?: number | string | null
  /** 量比 — fLianB */
  volume_ratio?: number | string | null
  /** 总市值(亿) — Zsz */
  total_market_value?: number | string | null
  /** 涨停价(元) — 来自 /stocks/{symbol}/l2 的 more.zt_price(non CN/l2 不可用时为 null) */
  zt_price?: number | string | null
}

/** 快照行纯函数: 缺值/脏值一律 `--`(不编, 不渲染 NaN)。格式化一律走 @/lib/format 的 safe* 系列(R6)。 */
export function mapSnapshot(
  q?: QuoteSnapshot | null,
  more?: MoreInfoSnapshot | null,
): SnapshotCell[] {
  const quote = q ?? {}
  const info = more ?? {}
  const changeNum = safeNum(quote.change_pct)
  const turnoverRate = safeNum(info.turnover_rate)
  const marketCap = safeNum(info.total_market_value)
  return [
    { key: 'price', label: '现价', value: safePrice(quote.current_price, 2) },
    {
      key: 'change_pct',
      label: '涨跌幅',
      value: changeNum == null ? '--' : `${changeNum >= 0 ? '+' : ''}${safeFixed(changeNum, 2)}%`,
    },
    { key: 'open', label: '今开', value: safePrice(quote.open_price, 2) },
    { key: 'high', label: '最高', value: safePrice(quote.high_price, 2) },
    { key: 'low', label: '最低', value: safePrice(quote.low_price, 2) },
    { key: 'amount', label: '成交额', value: fmtAmount(safeNum(quote.turnover)) },
    {
      key: 'turnover',
      label: '换手率',
      value: turnoverRate == null ? '--' : `${safePrice(turnoverRate, 2)}%`,
    },
    { key: 'volume_ratio', label: '量比', value: safePrice(safeNum(info.volume_ratio), 2) },
    {
      key: 'market_cap',
      label: '总市值',
      value: marketCap == null ? '--' : `${safePrice(marketCap, 2)}亿`,
    },
    { key: 'limit_price', label: '涨停价', value: safePrice(info.zt_price, 2) },
  ]
}

/** 现价/涨跌幅在顶行已醒目呈现 → 快照行去除, 守住「同一数据点只出现一处」(spec §一)。 */
const TOP_ROW_KEYS = new Set(['price', 'change_pct'])

const TYPE_OPTIONS: { id: WorkbenchType; label: string }[] = [
  { id: 'stock', label: '个股' },
  { id: 'index', label: '指数' },
  { id: 'board', label: '板块' },
]

interface L2MoreResp {
  more?: { zt_price?: number | null } | null
}

export interface HeaderBandProps {
  symbol: string
  market?: string
  type?: WorkbenchType
  hasPosition?: boolean
  onTypeChange?: (t: WorkbenchType) => void
  onGotoTab?: (tab: WorkbenchTab) => void
}

export default function HeaderBand({
  symbol,
  market = 'CN',
  type = 'stock',
  hasPosition = false,
  onTypeChange,
  onGotoTab,
}: HeaderBandProps) {
  const isStock = type === 'stock'
  const [quote, setQuote] = useState<QuoteSnapshot | null>(null)
  const [more, setMore] = useState<MoreInfoSnapshot | null>(null)
  const [ztPrice, setZtPrice] = useState<number | null>(null)
  const [summary, setSummary] = useState<KlineSummaryData | null>(null)
  const [busy, setBusy] = useState(false)
  const [tick, setTick] = useState(0)

  // 换股/换类型必须先清上一标的的旧值(否则会把"A股的涨停价"画到"指数"上);
  // 手动刷新(tick)不清, 以保留 stale-on-error 的旧值语义。
  useEffect(() => {
    setQuote(null)
    setMore(null)
    setZtPrice(null)
    setSummary(null)
  }, [symbol, market, isStock])

  useEffect(() => {
    if (!symbol) return
    let alive = true
    setBusy(true)
    const tasks: Promise<unknown>[] = [
      insightApi.quote<QuoteSnapshot>(symbol, market).then((r) => {
        if (alive) setQuote(r ?? null)
      }),
      insightApi.moreInfo<MoreInfoSnapshot>(symbol, market).then((r) => {
        if (alive) setMore(r ?? null)
      }),
    ]
    if (isStock) {
      // Ruling A: 涨停价自包含取 /stocks/{s}/l2(不能依赖兄弟组件回喂)
      tasks.push(
        fetchAPI<L2MoreResp>(`/stocks/${encodeURIComponent(symbol)}/l2`).then((r) => {
          if (alive) setZtPrice(r?.more?.zt_price ?? null)
        }),
      )
      tasks.push(
        insightApi.klineSummary<KlineSummaryData>(symbol, market).then((r) => {
          if (alive) setSummary(r ?? null)
        }),
      )
    }
    // 失败保留旧值(stale-on-error), 不编造、不清零
    void Promise.allSettled(tasks).then(() => {
      if (alive) setBusy(false)
    })
    return () => {
      alive = false
    }
  }, [symbol, market, isStock, tick])

  const refresh = useCallback(() => setTick((t) => t + 1), [])

  const suggestion = useMemo(
    () => (isStock && summary ? buildKlineSuggestion(summary, hasPosition) : null),
    [isStock, summary, hasPosition],
  )

  const changeNum = safeNum(quote?.change_pct)
  const toneClass =
    changeNum == null
      ? 'text-muted-foreground'
      : changeNum > 0
        ? 'text-[--stock-up]'
        : changeNum < 0
          ? 'text-[--stock-down]'
          : 'text-muted-foreground'
  const cells = mapSnapshot(quote, { ...(more ?? {}), zt_price: ztPrice }).filter(
    (c) => !TOP_ROW_KEYS.has(c.key),
  )
  const scoreText = suggestion ? `${suggestion.score >= 0 ? '+' : ''}${suggestion.score}` : '--'

  return (
    <div className="sticky top-0 z-20 rounded border border-border/60 bg-background/95 px-3 py-2 backdrop-blur">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-[15px] font-semibold">{quote?.name || symbol}</span>
        <span className="font-mono text-[11px] text-muted-foreground">{symbol}</span>
        <span className={cn('font-mono text-[18px] font-semibold', toneClass)}>
          {safePrice(quote?.current_price, 2)}
        </span>
        <span className={cn('font-mono text-[12px]', toneClass)}>
          {changeNum == null ? '--' : `${changeNum >= 0 ? '+' : ''}${safeFixed(changeNum, 2)}%`}
        </span>

        <div className="ml-auto flex items-center gap-1">
          <div className="flex items-center gap-0.5" role="group" aria-label="类型切换">
            {TYPE_OPTIONS.map((o) => (
              <button
                key={o.id}
                type="button"
                aria-pressed={type === o.id}
                onClick={() => {
                  if (type !== o.id) onTypeChange?.(o.id)
                }}
                className={cn(
                  'rounded px-1.5 py-0.5 text-[11px] transition-colors',
                  type === o.id
                    ? 'bg-accent text-foreground'
                    : 'text-muted-foreground hover:text-foreground',
                )}
              >
                {o.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={refresh}
            title="刷新"
            aria-label="刷新"
            className="rounded p-1 text-muted-foreground hover:text-foreground"
          >
            <RefreshCw className={cn('h-3.5 w-3.5', busy && 'animate-spin')} />
          </button>
        </div>
      </div>

      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1">
        {cells.map((c) => (
          <div key={c.key} className="flex items-baseline gap-1 text-[11px]">
            <span className="text-muted-foreground">{c.label}</span>
            <span className="font-mono">{c.value}</span>
          </div>
        ))}
      </div>

      {suggestion ? (
        <button
          type="button"
          onClick={() => onGotoTab?.('suggest')}
          className="mt-1.5 flex w-full items-center gap-2 rounded border border-border/60 px-2 py-1 text-left hover:bg-accent/40"
        >
          <TechnicalBadge
            label={`建议·${suggestion.action_label}`}
            tone={technicalToneFromSuggestionAction(suggestion.action, suggestion.action_label)}
            size="sm"
          />
          <span className="font-mono text-[11px] text-muted-foreground">评分 {scoreText}</span>
          <span className="truncate text-[11px]">{suggestion.signal}</span>
        </button>
      ) : null}
    </div>
  )
}
