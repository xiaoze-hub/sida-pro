import { useCallback, useEffect, useMemo, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { fetchAPI, insightApi } from '@panwatch/api'
import { cn } from '@panwatch/base-ui'
import { TechnicalBadge, technicalToneFromSuggestionAction } from '@panwatch/biz-ui/components/technical-badge'
import { fmtAmount } from '@panwatch/biz-ui/lib/ladder-format'
import { buildKlineSuggestion } from '@/lib/kline-scorer'
import { safeFixed, safeNum, safePrice } from '@/lib/format'
import type { KlineSummaryData } from '@panwatch/biz-ui/components/kline-summary-dialog'
import { showStockOnly, type WorkbenchTab, type WorkbenchType } from '@/lib/workbench-tabs'

/**
 * 带1 顶部信息带(v0.5.96, 三合一 spec §4.1): 个股/指数/板块**共享**。
 *
 * 结构: 顶行(名称 + 代码 + 现价 + 涨跌色 + 类型三按钮 + 刷新)
 *       + 快照行(今开/最高/最低/成交额/换手率/量比/总市值/流通市值/
 *                PE(动)/PE(TTM)/PB/股息率/涨停价/连板)
 *       + 技术指标买卖建议条(仅 type=stock; 点击跳「建议」标签)。
 *
 * 类型分流(spec 绑定条款): `type !== 'stock'` 时**隐藏**(非渲染成 `--`)7 个个股专属 cell ——
 * `float_market_cap`/`pe_dynamic`/`pe_ttm`/`pb`/`dividend_yield`/`limit_price`/`limit_boards`;
 * 指数/板块保留共享 cell(今开/最高/最低/成交额/换手率/量比/总市值)。
 * `mapSnapshot` 保持 type-agnostic（纯函数不动），分流在渲染侧 `visibleSnapshotCells`。
 *
 * 数据面(全部真数据, 缺值 `--`, 绝不编造):
 *  - GET /quotes/{symbol}            → current_price/change_pct/open_price/high_price/low_price/turnover
 *  - GET /quotes/{symbol}/more-info  → turnover_rate/volume_ratio/total_market_value(亿)/circulating_market_value(亿)
 *  - GET /stocks/{symbol}/l2         → more.zt_price(涨停价) + more.pe_dynamic/pe_ttm/pb/dividend_yield/ever_zt_count
 *                                      (自包含, 不等兄弟组件回喂)
 *  - GET /klines/{symbol}/summary    → buildKlineSuggestion(技术面建议)
 * **CN-only**: more-info 与 /l2 只在 `个股 + CN` 时发(`cnStockDataEnabled`)—— more-info 对非 CN 后端 400,
 * /l2 是 CN TQ RPC; 否则同代码的境外标的会把 CN 涨停价/PE/PB 画成自己的。
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
 * more-info 侧字段(packages/marketdata types.py::MoreInfo 透传)。
 * 注意 total_market_value / circulating_market_value 单位都是**亿**(Zsz/Ltsz 原值未换算),
 * 展示必须带「亿」, 不可再过 fmtAmount(元口径)。
 */
export interface MoreInfoSnapshot {
  /** 换手率(%) — fHSL */
  turnover_rate?: number | string | null
  /** 量比 — fLianB */
  volume_ratio?: number | string | null
  /** 总市值(亿) — Zsz */
  total_market_value?: number | string | null
  /** 流通市值(亿) — Ltsz */
  circulating_market_value?: number | string | null
}

/**
 * `/stocks/{symbol}/l2` 的 `more` 段(spec §1.2 快照行后半段的真数据面; 仅个股/CN 可用)。
 * 字段证据: src/core/stock_l2.py::fetch_more:75-98(`_f` 取原值, 不做单位换算) + 源键
 * packages/marketdata/.../vendors/tq.py::_parse_more_info:203-219(ZTPrice/DynaPE/StaticPE_TTM/PB_MRQ/DYRatio/EverZTCount)。
 */
export interface L2MoreSnapshot {
  /** 涨停价(元) — ZTPrice */
  zt_price?: number | string | null
  /** PE(动) — DynaPE */
  pe_dynamic?: number | string | null
  /** PE(TTM) — StaticPE_TTM */
  pe_ttm?: number | string | null
  /** PB(市净率) — PB_MRQ */
  pb?: number | string | null
  /** 股息率(%) — DYRatio */
  dividend_yield?: number | string | null
  /** 连板天数(个) — EverZTCount */
  ever_zt_count?: number | string | null
}

/** 快照行纯函数: 缺值/脏值一律 `--`(不编, 不渲染 NaN)。格式化一律走 @/lib/format 的 safe* 系列(R6)。 */
export function mapSnapshot(
  q?: QuoteSnapshot | null,
  more?: MoreInfoSnapshot | null,
  l2?: L2MoreSnapshot | null,
): SnapshotCell[] {
  const quote = q ?? {}
  const info = more ?? {}
  const l2m = l2 ?? {}
  const changeNum = safeNum(quote.change_pct)
  const turnoverRate = safeNum(info.turnover_rate)
  const marketCap = safeNum(info.total_market_value)
  const floatCap = safeNum(info.circulating_market_value)
  const dividendYield = safeNum(l2m.dividend_yield)
  const boards = safeNum(l2m.ever_zt_count)
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
    {
      key: 'float_market_cap',
      label: '流通市值',
      value: floatCap == null ? '--' : `${safePrice(floatCap, 2)}亿`,
    },
    // 估值三件套: 原样数值(PE/PB 可为负 = 亏损股真实口径), 缺值 `--`
    { key: 'pe_dynamic', label: 'PE(动)', value: safePrice(safeNum(l2m.pe_dynamic), 2) },
    { key: 'pe_ttm', label: 'PE(TTM)', value: safePrice(safeNum(l2m.pe_ttm), 2) },
    { key: 'pb', label: 'PB', value: safePrice(safeNum(l2m.pb), 2) },
    {
      key: 'dividend_yield',
      label: '股息率',
      value: dividendYield == null ? '--' : `${safePrice(dividendYield, 2)}%`,
    },
    { key: 'limit_price', label: '涨停价', value: safePrice(l2m.zt_price, 2) },
    // 连板: 整数天(safeFixed(...,0) 四舍五入, 不加千分位——连板数上限个位数)
    { key: 'limit_boards', label: '连板', value: safeFixed(boards, 0) },
  ]
}

/** 现价/涨跌幅在顶行已醒目呈现 → 快照行去除, 守住「同一数据点只出现一处」(spec §一)。 */
const TOP_ROW_KEYS = new Set(['price', 'change_pct'])

/**
 * 个股专属 cell(仅 `type === 'stock'` 显示): 涨停价/连板来自 CN-only 的 `/stocks/{s}/l2`,
 * 流通市值/PE/PB/股息率是个股估值口径 —— 指数/板块无此概念, 渲染成 `--` 只是噪声,
 * 故**隐藏**而非占位。与 CN-only 数据面闸门(`cnStockDataEnabled`)同源:
 * 非同 CN 标的这些格本就取不到值, 一并消失比留 7 个 `--` 更诚实。
 * 过滤在**渲染侧**(不在 `mapSnapshot` 内做类型分流, 保持纯函数 type-agnostic)。
 */
const EQUITY_ONLY_KEYS = new Set([
  'float_market_cap',
  'pe_dynamic',
  'pe_ttm',
  'pb',
  'dividend_yield',
  'limit_price',
  'limit_boards',
])

/**
 * 渲染侧可见 cell(纯函数, 供单测锚定): 顶行去重 + 个股专属 cell 的**类型分流**。
 * 非个股直接**不渲染** 7 个估值/涨停/连板 cell(而非渲染成 `--`)。
 * `mapSnapshot` 保持 type-agnostic 不变。
 */
export function visibleSnapshotCells(
  q?: QuoteSnapshot | null,
  more?: MoreInfoSnapshot | null,
  l2?: L2MoreSnapshot | null,
  isStock = true,
): SnapshotCell[] {
  return mapSnapshot(q, more, l2).filter(
    (c) => !TOP_ROW_KEYS.has(c.key) && (isStock || !EQUITY_ONLY_KEYS.has(c.key)),
  )
}

/**
 * CN-only 数据面闸门(纯函数, 供单测锚定): `/quotes/{s}/more-info` 对非 CN 后端直接 400,
 * `/stocks/{s}/l2` 是 CN TQ RPC —— 只有「个股 + CN」才允许发这两个请求,
 * 否则同代码的境外标的会把 CN 涨停价/PE/PB 画成自己的。
 */
export function cnStockDataEnabled(t?: WorkbenchType, market?: string): boolean {
  return showStockOnly(t ?? 'stock') && (market ?? 'CN') === 'CN'
}

const TYPE_OPTIONS: { id: WorkbenchType; label: string }[] = [
  { id: 'stock', label: '个股' },
  { id: 'index', label: '指数' },
  { id: 'board', label: '板块' },
]

interface L2MoreResp {
  more?: L2MoreSnapshot | null
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
  const isStock = showStockOnly(type)
  /** CN-only 数据面闸门: `/stocks/{s}/l2` 是 CN TQ RPC, more-info 对非 CN 直接 400 —— 同代码的境外标的绝不能发, 否则会把 CN 涨停价/PE/PB 画到别的标的上。 */
  const cnStock = cnStockDataEnabled(type, market)
  const [quote, setQuote] = useState<QuoteSnapshot | null>(null)
  const [more, setMore] = useState<MoreInfoSnapshot | null>(null)
  const [l2More, setL2More] = useState<L2MoreSnapshot | null>(null)
  const [summary, setSummary] = useState<KlineSummaryData | null>(null)
  const [busy, setBusy] = useState(false)
  const [tick, setTick] = useState(0)

  // 换股/换类型必须先清上一标的的旧值(否则会把"A股的涨停价"画到"指数"上);
  // 手动刷新(tick)不清, 以保留 stale-on-error 的旧值语义。
  useEffect(() => {
    setQuote(null)
    setMore(null)
    setL2More(null)
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
    ]
    if (cnStock) {
      // CN-only 数据面(非同 CN 标的不得发): more-info 对非 CN 后端 400;
      // /stocks/{s}/l2 是 CN TQ RPC → 涨停价/PE/PB/股息率/连板 只对 CN 个股有意义。
      tasks.push(
        insightApi.moreInfo<MoreInfoSnapshot>(symbol, market).then((r) => {
          if (alive) setMore(r ?? null)
        }),
      )
      // Ruling A: 涨停价/PE/PB/股息率/连板 自包含取 /stocks/{s}/l2(不能依赖兄弟组件回喂)
      tasks.push(
        fetchAPI<L2MoreResp>(`/stocks/${encodeURIComponent(symbol)}/l2`).then((r) => {
          if (alive) setL2More(r?.more ?? null)
        }),
      )
    }
    if (isStock) {
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
  }, [symbol, market, isStock, cnStock, tick])

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
  // 顶行去重(price/change_pct) + 个股专属 cell 的**类型分流**:
  // index/board 直接不渲染估值/涨停/连板(它们是 -- 噪声, 且指数/板块无此概念), 而非渲染成 `--`。
  const cells = visibleSnapshotCells(quote, more, l2More, isStock)
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
