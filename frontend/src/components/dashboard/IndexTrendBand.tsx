import { useCallback, useEffect, useRef, useState } from 'react'

import { fetchAPI } from '@panwatch/api'
import { safeFixed } from '@/lib/format'
import MinuteLwcChart, { type MinutePoint } from '@panwatch/biz-ui/components/MinuteLwcChart'

/**
 * 大盘实时走势带（2026-10-24，替换总结卡右侧的大片留白）。
 *
 * 为什么不是"一段文字分析"：
 *   产品铁律是「看图决策，不做数据堆砌」。一段 LLM 写的"震荡上行、资金回暖"是不可验算的叙述，
 *   且必然夹带无数据支撑的判断词（用户对编造情绪/意图零容忍）。所以这里给**图形 + 可算的事实**，
 *   想看解释去问参谋。
 *
 * 数据：复用既有 `/quotes/minute/{symbol}`（腾讯分时，240 点，零 key）。
 *   该端点对指数已 `is_index=true`，后端不再算均价（指数没有"成交均价"语义），
 *   前端 MinuteLwcChart 的 `if (!isIndex)` 分支也已跳过均价线与 swings —— 不需要新接口。
 *
 * 三个事实全部由 points 现算，没有一个判断词：
 *   日内位置 = (现价-今低)/(今高-今低)
 *   尾盘30分 = 现价/14:30 价 - 1
 *   午后/上午量 = 13:00-15:00 量 / 09:30-11:30 量
 *
 * 诚实口径：加载中/失败/源降级/当日真空 四态分开，绝不画一条假的平线。
 * 基准日：接口返回 `trade_date`（腾讯 payload 自带）。休市时显示「基准日 2026-10-23（非今日）」。
 */

const INDICES = [
  { symbol: '000001', name: '上证' },
  { symbol: '399001', name: '深成' },
  { symbol: '399006', name: '创业板' },
] as const

type Resp = {
  points: MinutePoint[]
  prev_close: number | null
  is_index: boolean
  degraded: boolean
  note: string | null
  trade_date: string | null
}

const fmtDate = (d: string | null) =>
  d && d.length === 8 ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6)}` : null

const pctText = (v: number) => `${v >= 0 ? '+' : ''}${safeFixed(v, 2)}%`

/** 上午收盘的点位下标：09:30 起算到 11:30 共 121 个点（含 11:30） */
const AM_BARS = 121

export type TrendFacts = {
  position: number | null // 0–100，日内位置
  positionRange: [number, number] | null // [今低, 今高]
  tail30: number | null // 尾盘 30 分钟涨跌幅（%）
  pmAmRatio: number | null // 午后量 / 上午量
}

/** 纯函数：可单独测试，不依赖 DOM */
export function computeFacts(points: MinutePoint[]): TrendFacts {
  if (!points.length) {
    return { position: null, positionRange: null, tail30: null, pmAmRatio: null }
  }
  const prices = points.map((p) => p.price).filter((v) => Number.isFinite(v))
  if (!prices.length) {
    return { position: null, positionRange: null, tail30: null, pmAmRatio: null }
  }
  const hi = Math.max(...prices)
  const lo = Math.min(...prices)
  const last = prices[prices.length - 1]
  const position = hi > lo ? ((last - lo) / (hi - lo)) * 100 : null

  // 尾盘 30 分钟：找最后一个 <= 1430 的点作为基准
  let ref: number | null = null
  for (const p of points) {
    if (p.t <= '1430') ref = p.price
    else break
  }
  const tail30 = ref && ref > 0 ? (last / ref - 1) * 100 : null

  // 午后 / 上午 量比（同日内部对比，不需要历史基线）
  const vols = points.map((p) => (Number.isFinite(p.volume) ? p.volume : 0))
  const am = vols.slice(0, AM_BARS).reduce((a, b) => a + b, 0)
  const pm = vols.slice(AM_BARS).reduce((a, b) => a + b, 0)
  const pmAmRatio = am > 0 && pm > 0 ? pm / am : null

  return { position, positionRange: hi > lo ? [lo, hi] : null, tail30, pmAmRatio }
}

export default function IndexTrendBand({ defaultSymbol = '000001' }: { defaultSymbol?: string }) {
  const [symbol, setSymbol] = useState<string>(defaultSymbol)
  const [data, setData] = useState<Resp | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const seqRef = useRef(0)

  const load = useCallback(async () => {
    const seq = ++seqRef.current
    setLoading(true)
    setError('')
    try {
      const res = await fetchAPI<Resp>(
        `/quotes/minute/${encodeURIComponent(symbol)}?market=CN`,
        { cacheMode: 'reload', timeoutMs: 60000 },
      )
      if (seq !== seqRef.current) return
      // 兜底: 响应体缺 points(或非数组)时按空处理 —— 首页是首屏, 不能因一个字段
      // 缺失就整块白屏。真空态走 `当日无分时` 分支, 不伪装成有数据。
      setData({ ...res, points: Array.isArray(res?.points) ? res.points : [] })
    } catch (e) {
      if (seq !== seqRef.current) return
      setError(e instanceof Error ? e.message : '加载失败')
      setData(null)
    } finally {
      if (seq === seqRef.current) setLoading(false)
    }
  }, [symbol])

  useEffect(() => {
    // 显式接住 rejection: load 内部已 try/catch, 但 void 丢弃的 promise 在 jsdom
    // 下仍会被算作 unhandled(测试里表现为"Error: 502"逃逸)
    load().catch(() => {})
  }, [load])

  const facts = computeFacts(data?.points ?? [])
  const dateLabel = fmtDate(data?.trade_date ?? null)
  const current = INDICES.find((i) => i.symbol === symbol) ?? INDICES[0]

  return (
    <section
      data-testid="index-trend-band"
      className="flex min-w-0 flex-col gap-1.5 rounded-md bg-s1 p-2"
      aria-label="大盘实时走势"
    >
      <div className="flex items-center gap-2">
        <span className="text-[12px] font-medium text-foreground">{current.name}分时</span>
        <div className="flex items-center gap-0.5" role="tablist" aria-label="切换指数">
          {INDICES.map((i) => (
            <button
              key={i.symbol}
              type="button"
              role="tab"
              aria-selected={i.symbol === symbol}
              data-testid={`trend-tab-${i.symbol}`}
              onClick={() => setSymbol(i.symbol)}
              className={`rounded px-1.5 py-0.5 text-[12px] transition-colors ${
                i.symbol === symbol
                  ? 'bg-accent text-foreground'
                  : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              {i.name}
            </button>
          ))}
        </div>
        <span className="ml-auto shrink-0 text-[12px] text-muted-foreground">
          {dateLabel ? `基准日 ${dateLabel}` : '最近交易日'}
        </span>
      </div>

      {loading && !data && (
        <div
          data-testid="trend-loading"
          className="flex items-center justify-center text-[12px] text-muted-foreground"
          style={{ height: 120 }}
        >
          分时加载中…
        </div>
      )}

      {error && !loading && (
        <div
          data-testid="trend-error"
          className="flex flex-col items-center justify-center gap-1 text-[12px] text-red-500"
          style={{ height: 120 }}
        >
          <span>分时取数失败：{error}</span>
          <button
            type="button"
            onClick={() => load().catch(() => {})}
            className="rounded border border-border px-2 py-0.5 text-[12px] text-foreground"
          >
            重试
          </button>
        </div>
      )}

      {!error && data?.degraded && (
        <div
          data-testid="trend-degraded"
          className="flex items-center justify-center text-[12px] text-amber-500"
          style={{ height: 120 }}
        >
          分时源异常：{data.note || '数据源暂不可用'}（空列表是源故障，不等于停牌）
        </div>
      )}

      {!error && !data?.degraded && data && data.points.length > 0 && (
        <>
          <MinuteLwcChart
            points={data.points}
            prevClose={data.prev_close}
            isIndex={!!data.is_index}
            height={120}
          />
          <dl className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[12px]">
            <Fact
              label="日内位置"
              value={facts.position == null ? '—' : `${safeFixed(facts.position, 0)}%`}
              title={
                facts.positionRange
                  ? `区间 ${safeFixed(facts.positionRange[0], 2)} – ${safeFixed(facts.positionRange[1], 2)}`
                  : '当日无振幅'
              }
              testid="fact-position"
            />
            <Fact
              label="尾盘30分"
              value={facts.tail30 == null ? '—' : pctText(facts.tail30)}
              tone={facts.tail30 == null ? 'muted' : facts.tail30 >= 0 ? 'up' : 'down'}
              testid="fact-tail30"
            />
            <Fact
              label="午后/上午量"
              value={facts.pmAmRatio == null ? '—' : safeFixed(facts.pmAmRatio, 2)}
              testid="fact-pm-am"
            />
          </dl>
        </>
      )}

      {!error && !data?.degraded && (!data || data.points.length === 0) && !loading && (
        <div
          data-testid="trend-empty"
          className="flex items-center justify-center text-[12px] text-muted-foreground"
          style={{ height: 120 }}
        >
          当日无分时（非交易日 / 停牌）
        </div>
      )}
    </section>
  )
}

function Fact({
  label,
  value,
  title,
  tone = 'muted',
  testid,
}: {
  label: string
  value: string
  title?: string
  tone?: 'up' | 'down' | 'muted'
  testid?: string
}) {
  const color =
    tone === 'up' ? 'text-stock-up' : tone === 'down' ? 'text-stock-down' : 'text-foreground'
  return (
    <div className="flex items-baseline gap-1" data-testid={testid} title={title}>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className={`font-num tabular-nums ${color}`}>{value}</dd>
    </div>
  )
}
