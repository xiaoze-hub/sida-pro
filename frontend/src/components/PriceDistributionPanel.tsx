import { safeFixed, safeNum, toAmount } from '@/lib/format'

/**
 * I-8 微观分价博弈矩阵（纯展示组件, 不取数、不重算口径）。
 *
 * 为什么单独一个文件: L2Tab 是取数容器（InsightProvider + 轮询 + 读穿透缓存），
 * 而这块所有判定（托举位/抛压带/拆单）都由后端 `/api/market/price-distribution/{symbol}`
 * 算好并落库。前端**只渲染**——两处算法必然分叉, 且阈值一旦不一致就会互相打脸,
 * 回测口径也会跟着崩。
 *
 * 视觉契约:
 *  · 顶部三条结论（🛡️托举 / ⚡抛压 / 🕵️拆单）是后端 `summary[].text` 原样展示;
 *  · 中部「多空梯队」每价位一根红绿对比条: **条长 = 该档成交量占全日比**（谁在放量）,
 *    **红绿比例 = 主动买/主动卖**（谁在主导）——两个维度一条条看全;
 *  · 缺值一律 `--`（走 safeFixed/safeNum）, 绝不把"没数据"画成 0（0 是有效信息）。
 *
 * 颜色: 用 `--gs-go / --gs-stop` 令牌（与 K 线买卖点同源, 红买绿卖）, 不写 hex、
 * 也不用涨跌色作背景类（设计系统 R12: 背景只表达空间, 涨跌用图形/文字表达）。
 */

export interface PriceBin {
  price: number
  volume?: number | null
  buy_vol?: number | null
  sell_vol?: number | null
  big_vol?: number | null
  buy_ratio?: number | null
  sell_ratio?: number | null
  big_ratio?: number | null
  net_vol?: number | null
  net_amount?: number | null
  day_share?: number | null
  tags?: string[]
}

export interface PriceLevel {
  price: number
  buy_ratio?: number | null
  sell_ratio?: number | null
  volume?: number | null
  net_amount?: number | null
  big_ratio?: number | null
  side?: 'buy' | 'sell' | null
  label?: string | null
}

export interface PriceSummaryItem {
  key: 'support' | 'resistance' | 'iceberg' | string
  icon?: string
  title?: string
  available?: boolean
  price?: number | null
  ratio?: number | null
  text?: string | null
}

export interface PriceDistributionDay {
  total_vol?: number | null
  day_total_vol?: number | null
  buy_vol?: number | null
  sell_vol?: number | null
  net_vol?: number | null
  net_amount?: number | null
  buy_ratio?: number | null
  big_ratio?: number | null
  vwap?: number | null
  coverage?: number | null
}

export interface PriceDistributionQuality {
  bins?: number
  identity_ok_ratio?: number | null
  big_data_present?: boolean
  coverage?: number | null
  degraded?: boolean
  cross_check?: {
    price_table_buy_ratio?: number | null
    quote_outer_ratio?: number | null
    diff_pp?: number | null
    note?: string
  } | null
}

export interface PriceDistributionCache {
  hit?: boolean
  source?: string
  age_s?: number | null
  ttl_s?: number | null
  stale?: boolean
  reason?: string | null
}

export interface PriceDistribution {
  available: boolean
  reason?: string | null
  symbol?: string
  market?: string
  name?: string | null
  trade_date?: string | null
  snapshot_ts?: string | null
  quote?: { close?: number | null; change_pct?: number | null } | null
  day?: PriceDistributionDay | null
  bins?: PriceBin[]
  support_levels?: PriceLevel[]
  resistance_levels?: PriceLevel[]
  iceberg_signals?: PriceLevel[]
  summary?: PriceSummaryItem[]
  bias?: { code?: string; label?: string; net_amount?: number | null } | null
  quality?: PriceDistributionQuality | null
  cache?: PriceDistributionCache | null
  caliber?: string | null
}

/** 比例(0~1) → 百分数文本; 缺值 `--`。 */
function pctText(v: unknown, digits = 1): string {
  const n = safeNum(v)
  return n == null ? '--' : `${safeFixed(n * 100, digits)}%`
}

/** 手 → 带单位的量; 缺值 `--`。 */
function volText(v: unknown): string {
  const n = safeNum(v)
  if (n == null) return '--'
  if (Math.abs(n) >= 1e4) return `${safeFixed(n / 1e4, 2)}万手`
  return `${safeFixed(n, 0)}手`
}

const TAG_TEXT: Record<string, string> = {
  strong_support: '托举',
  strong_resistance: '抛压',
  iceberg_buy: '拆单吸筹',
  iceberg_sell: '拆单派发',
  big_heavy: '大单',
  undirected: '竞价',
  single_side: '单向',
}

const TAG_HINT: Record<string, string> = {
  strong_support: '成交量前 3 且竞买率 ≥ 65%: 放量主动买承接',
  strong_resistance: '成交量前 3 且竞买率 ≤ 35%: 放量主动卖压制',
  iceberg_buy: '巨量档但大单缺席 + 主动买占优: 疑似小单拆单吸筹',
  iceberg_sell: '巨量档但大单缺席 + 主动卖占优: 疑似小单拆单派发',
  big_heavy: '该档大单占比 ≥ 35%',
  undirected: '开盘/收盘价档混入集合竞价(无主动方), 不计入多空判定; 成交量仍计入全日',
  single_side: '该价位只有单边主动成交, 方向真实但样本单薄',
}

function tagClass(tag: string): string {
  if (tag === 'strong_support' || tag === 'iceberg_buy') return 'text-stock-up'
  if (tag === 'strong_resistance' || tag === 'iceberg_sell') return 'text-stock-down'
  return 'text-muted-foreground'
}

/**
 * 多空梯队: 条长按**成交量占全日比**归一（谁放量）, 条内红绿按竞买率切（谁主导）。
 *
 * 为什么不按价格等距画: 分价表的价档是离散的, 等距会在跳空处产生大片空白,
 * 反而看不出"量堆在哪"。这里按量归一 + 价格降序排列, 一屏能看到量堆与方向。
 */
export function PriceDistributionLadder({
  bins,
  maxBins = 24,
}: {
  bins: PriceBin[]
  maxBins?: number
}) {
  const shown = [...bins]
    .filter((b) => safeNum(b.price) != null)
    .sort((a, b) => (safeNum(b.price) ?? 0) - (safeNum(a.price) ?? 0))
    .slice(0, maxBins)
  const maxShare = Math.max(...shown.map((b) => safeNum(b.day_share) ?? 0), 0.0001)

  return (
    <div className="space-y-0.5" data-testid="price-dist-ladder">
      {shown.map((b) => {
        const buy = safeNum(b.buy_ratio)
        const share = safeNum(b.day_share)
        const widthPct = Math.min(100, Math.max(2, ((share ?? 0) / maxShare) * 100))
        const hasRatio = buy != null
        const buyW = Math.min(100, Math.max(0, (buy ?? 0) * 100))
        const tags = b.tags ?? []
        const net = safeNum(b.net_amount)
        return (
          <div
            key={b.price}
            data-testid="price-dist-row"
            data-price={b.price}
            className="flex items-center gap-2 text-[12px]"
            title={`${safeFixed(b.price, 2)} 元 · 成交 ${volText(b.volume)} · 主动买 ${pctText(
              buy,
            )} / 主动卖 ${pctText(b.sell_ratio)} · 大单占比 ${pctText(
              b.big_ratio,
            )} · 净额 ${toAmount(net)}`}
          >
            <span className="w-12 shrink-0 font-mono text-foreground">{safeFixed(b.price, 2)}</span>
            <span className="flex min-w-0 flex-1 items-center gap-1">
              <span className="min-w-0" style={{ width: `${widthPct}%` }}>
                <span className="flex h-2.5 w-full overflow-hidden rounded-sm bg-s2">
                  {hasRatio ? (
                    <>
                      <span
                        data-testid="price-dist-bar-buy"
                        className="h-full"
                        style={{ width: `${buyW}%`, backgroundColor: 'var(--gs-go)' }}
                      />
                      <span
                        data-testid="price-dist-bar-sell"
                        className="h-full"
                        style={{ width: `${100 - buyW}%`, backgroundColor: 'var(--gs-stop)' }}
                      />
                    </>
                  ) : (
                    /* 竞买率缺值: 画中性灰条, **不**画成 100% 卖(缺失不是方向) */
                    <span
                      data-testid="price-dist-bar-unknown"
                      className="h-full w-full bg-s3"
                    />
                  )}
                </span>
              </span>
            </span>
            <span className="w-10 shrink-0 text-right font-mono text-muted-foreground">
              {pctText(share, 1)}
            </span>
            <span className="w-16 shrink-0 text-right font-mono text-muted-foreground">
              {volText(b.volume)}
            </span>
            <span
              className={`w-16 shrink-0 text-right font-mono ${
                net == null ? 'text-muted-foreground' : net >= 0 ? 'text-stock-up' : 'text-stock-down'
              }`}
            >
              {toAmount(net)}
            </span>
            <span className="w-16 shrink-0 text-right">
              {tags.map((t) => (
                <span
                  key={t}
                  title={TAG_HINT[t] ?? t}
                  className={`ml-0.5 cursor-help ${tagClass(t)}`}
                >
                  {TAG_TEXT[t] ?? t}
                </span>
              ))}
            </span>
          </div>
        )
      })}
    </div>
  )
}

/** 顶部三条高确定性结论。文案全部来自后端, 前端只上色/排布。 */
export function PriceDistributionConclusions({ items }: { items: PriceSummaryItem[] }) {
  return (
    <div className="space-y-1" data-testid="price-dist-conclusions">
      {items.map((s) => (
        <div
          key={s.key}
          data-testid={`price-dist-summary-${s.key}`}
          data-available={s.available ? '1' : '0'}
          className="flex items-baseline gap-1.5 text-[12px]"
        >
          <span className="w-14 shrink-0 text-muted-foreground">
            {s.icon} {s.title}
          </span>
          <span className={s.available ? 'text-foreground' : 'text-muted-foreground'}>
            {s.text ?? '--'}
          </span>
        </div>
      ))}
    </div>
  )
}

export default function PriceDistributionPanel({
  data,
  loading,
  failed,
  lastOkAt,
  maxBins = 24,
}: {
  data: PriceDistribution | null
  loading: boolean
  failed: boolean
  lastOkAt: string
  maxBins?: number
}) {
  if (!data) {
    return (
      <div data-testid="price-dist-empty" className="text-[12px] text-muted-foreground">
        {loading ? '加载中…' : failed ? `取数失败（${lastOkAt ? `上次成功 ${lastOkAt}` : '暂无成功记录'}）` : '暂无数据'}
      </div>
    )
  }

  if (!data.available) {
    return (
      <div data-testid="price-dist-unavailable" className="space-y-1">
        <div className="text-[12px] text-amber-600 dark:text-amber-400">
          {data.reason || '分价矩阵不可用（不冒充 0）'}
        </div>
        {data.caliber ? (
          <div className="text-[12px] text-muted-foreground">{data.caliber}</div>
        ) : null}
      </div>
    )
  }

  const day = data.day
  const cache = data.cache
  const quality = data.quality
  const summary = data.summary ?? []
  const bins = data.bins ?? []
  const stale = cache?.stale === true || failed
  const net = safeNum(day?.net_amount)

  return (
    <div className="space-y-2" data-testid="price-dist-panel">
      {/* 结论区: 三条一句话结论(后端原样) */}
      <PriceDistributionConclusions items={summary} />

      {/* 数据血统: 交易日/快照时刻/缓存来源/偏离交叉披露 */}
      <div className="flex flex-wrap items-center gap-x-2 text-[12px] text-muted-foreground">
        <span>{data.trade_date ?? '--'}</span>
        {data.snapshot_ts ? <span>· 快照 {data.snapshot_ts}</span> : null}
        <span>
          · {cache?.source === 'db' ? `库缓存 ${safeFixed(cache?.age_s, 0)}s` : cache?.source === 'db_stale' ? '库·旧快照' : '实时取数'}
        </span>
        {quality?.degraded ? <span className="text-amber-500">· 逐档恒等式异常</span> : null}
        {stale ? (
          <span data-testid="price-dist-stale" className="text-amber-500">
            · 显示上次成功值{cache?.reason ? `（${cache.reason}）` : ''}
          </span>
        ) : null}
      </div>

      {/* 全日多空 + 量能锚 */}
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[12px] md:grid-cols-4">
        <div title="竞买量 / 总成交量(腾讯分价表原页面口径)">
          <div className="text-muted-foreground">全日竞买率</div>
          <div className="font-mono text-foreground">{pctText(day?.buy_ratio)}</div>
        </div>
        <div title="(竞买量 − 对手量) × 100 股 × 成交价, 元">
          <div className="text-muted-foreground">净买入额</div>
          <div className={`font-mono ${net == null ? 'text-muted-foreground' : net >= 0 ? 'text-stock-up' : 'text-stock-down'}`}>
            {toAmount(net)}
          </div>
        </div>
        <div title="矩阵覆盖档成交量合计 / 全日成交量">
          <div className="text-muted-foreground">档位覆盖</div>
          <div className="font-mono text-foreground">{pctText(day?.coverage, 0)}</div>
        </div>
        <div title="∑(价×量)/∑量, 仅覆盖档口径">
          <div className="text-muted-foreground">分价 VWAP</div>
          <div className="font-mono text-foreground">{safeFixed(day?.vwap, 2)}</div>
        </div>
      </div>

      {/* 多空梯队 */}
      <div className="space-y-1">
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <span className="w-12 shrink-0">价位</span>
          <span className="flex-1">多空博弈（条长=成交量占比, 红=主动买 / 绿=主动卖）</span>
          <span className="w-10 text-right">占比</span>
          <span className="w-16 text-right">成交量</span>
          <span className="w-16 text-right">净额</span>
          <span className="w-16 text-right">标记</span>
        </div>
        {bins.length > 0 ? (
          <PriceDistributionLadder bins={bins} maxBins={maxBins} />
        ) : (
          <div className="text-[12px] text-muted-foreground">无有效价档</div>
        )}
      </div>

      {/* 口径与体检（不藏起来: 回测/复盘时要能复现） */}
      <div className="flex flex-wrap items-center gap-x-2 text-[12px] text-muted-foreground">
        <span>档位 {quality?.bins ?? bins.length}</span>
        {quality?.identity_ok_ratio != null ? (
          <span>· 恒等式 {pctText(quality.identity_ok_ratio, 1)}</span>
        ) : null}
        {quality?.big_data_present === false ? <span>· 源未给大单列</span> : null}
        {data.bias?.label ? <span>· {data.bias.label}</span> : null}
        {quality?.cross_check ? (
          <span title={quality.cross_check.note}>
            · 外盘口径 {pctText(quality.cross_check.quote_outer_ratio)}（差
            {safeFixed(quality.cross_check.diff_pp, 1)}pp, 不互校）
          </span>
        ) : null}
        {data.caliber ? <span className="truncate">· {data.caliber}</span> : null}
      </div>
    </div>
  )
}
