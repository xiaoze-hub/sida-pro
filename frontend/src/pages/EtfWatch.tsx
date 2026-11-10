/**
 * 国家队 ETF 看板（2026-11-05）—— 对标「用 Tushare 复刻国家队看板」那套口径，
 * 数据源换成**通达信 TQ 本地网关**（零配额、零外部依赖）。
 *
 * ## 三层，各自的"为什么看"
 * 1. **盘中分钟观测**：20 只重点宽基 ETF 每分钟成交额加总，和上证综指放一起看。
 *    意义：国家队进场往往先体现在**宽基 ETF 的成交额突然放大**，而不是个股涨停。
 *    基线只取「同日 20/20 只齐备、金额非空、且覆盖本次同一批分钟」的交易日，按
 *    「同一分钟集合的累计」取日均 —— 比的是同日同期，不是全天，否则早盘永远"清淡"、
 *    尾盘永远"放量"，结论全是假的。16/20 只的日子不算样本：它的合计天然少 4 只的钱，
 *    拿它当基线会把今日的同样成交额算成 1.25 倍「偏活跃」（2026-11-10 复审反例）。
 *    缺只/金额 NULL/缺分钟/基线无同日同口径样本 → 一律不给活跃度结论，只报覆盖。
 * 2. **份额信号（SC8）**：市场级 SC8 两列**推断**为全市场 ETF 总份额与当日份额变动
 *    （单位推断为亿份，不是金额）。没有官方字段定义，也没有可换算的金额阈值：
 *    **不做托底日判定**，只报原始样本与样本内分位（推断依据见接口的 unit_evidence）。
 *    SC8 上游序列已停更（库内止于 2026-10-16），接口给 `stale/as_of`，
 *    页面必须带真实日期展示并标滞后，**不许**把停更日当今天。
 * 3. **日度全景**：20 只 ETF 按跟踪指数分 12 类的成交额。
 * 4. **分钟来源按库内 `etf_minute_flow.source` 逐日展示**：通达信 TQ / 东财 trends2 兜底 /
 *    混合 / 未知（NULL）都照实标，绝不写死「通达信 TQ」。混源基线只作参考。
 *
 * ## 口径纪律（不许违反）
 * - **单只 ETF 净申购不存在**：通达信 ETF 份额是"当前值回填到每一天"（实测 63 个交易日
 *   零变化），算出来恒为 0。所以只给市场级 SC8 份额（份额变动），页面上必须显式说明，不能用别的算法凑。
 * - 数据缺失一律显示 `--` + 原因，不写 0、不猜。
 * - 单位：成交额亿元；SC8 份额变动/总份额 **推断为亿份**（不得写成亿元，也不得乘净值换算）。数字一律 tabular-nums。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, Info, Loader2, RefreshCw } from 'lucide-react'
import { fetchAPI } from '@sida/api'
import PageHeader from '@/components/PageHeader'
import { safeFixed, safeNetInflow } from '@/lib/format'
import { cn } from '@/lib/utils'

type IntradayPoint = {
  minute: string
  amount_yi: number | null
  cum_yi: number
  baseline_cum_yi: number | null
  complete: boolean
}
type IntradayResp = {
  date: string
  /** 数据日相位（回退到历史日时是那天的相位，不是当前时段）。 */
  phase: string
  /** 当前时段相位（pre/live/closed/non_trading_day）。 */
  session_phase?: string
  session_time?: string
  /** 数据不是当日的（已回退），页面上必须标明「不是今天的行情」。 */
  stale?: boolean
  state: string
  /** 当日尚无分钟数据时回退到的最近交易日；有值即说明「显示的不是今天」。 */
  fallback_from?: string | null
  series: IntradayPoint[]
  totals: null | {
    cum_yi: number | null
    baseline_avg_yi: number | null
    ratio_vs_baseline: number | null
    /** 有没有得出活跃度结论：false 时 baseline_avg_yi/ratio_vs_baseline 一律为 null。 */
    conclusive?: boolean
    /** 不给结论的原因码（与后端 WITHHELD_LABELS 对应）。 */
    withheld_reasons?: string[]
    baseline_days: number
    baseline_days_requested?: number
    baseline_missing_days?: number
    baseline_days_dropped?: number
    /** 被剔除的基线日：篮子缺只 / 缺分钟（非同日同口径的一律不算样本）。 */
    baseline_days_incomplete_basket?: number
    baseline_days_minute_gap?: number
    /** 比较用的同一分钟集合大小（两侧都只累加这几根）。 */
    baseline_minutes_compared?: number
    baseline_sample_days?: number
    baseline_confirmed?: boolean
    last_minute: string | null
    last_observed_minute?: string | null
    in_progress_minute?: string | null
    etfs_expected: number
    /** 20/20 只齐备（金额非空）的分钟数；旧名 full_minutes/strict_minutes 仍兼容。 */
    complete_minutes?: number
    full_minutes: number
    strict_minutes?: number
    min_etfs_for_complete?: number
    /** 篮子缺只的分钟数（该分钟合计是残缺的）。 */
    minutes_short_basket?: number
    /** 金额全 NULL 的分钟数（未落库，不补 0）。 */
    minutes_without_amount?: number
    /** 已采集分钟之间的缺口数（缺一分钟就让累计少一截）。 */
    minute_gaps?: number
    minute_gaps_detail?: string[]
    minutes: number
    coverage?: number | null
    coverage_min?: number
  }
  benchmark?: { code: string; name: string; points: { minute: string; close: number }[] }
  note?: string
  /** 分钟表真实来源：tdx_tq / em_trends2 / mixed / unknown / no_data。 */
  source?: string
  sources?: string[]
  source_labels?: string[]
  /** 逐日来源（混源时逐日不同）。 */
  source_by_date?: Record<string, string[]>
  source_note?: string
  /** 基线窗口的来源：非 TQ 时比值只能当参考。 */
  baseline_sources?: string[]
  baseline_source_labels?: string[]
  baseline_source_note?: string
}
type SupportResp = {
  state: string
  /** 库内真实最新日（不上移、不改写）。 */
  as_of?: string
  /** 最新日超出 T+1 容差 → 页面必须显式提示「不是当前读数」。 */
  stale?: boolean
  freshness?: {
    as_of?: string | null
    checked_at?: string
    lag_trading_days?: number | null
    max_lag_trading_days?: number
    verified?: boolean
    stale?: boolean
    reason?: string | null
  }
  /** SC8 单位：**推断**为份额（亿份）。 */
  unit?: string
  scale_unit?: string
  net_sub_unit?: string
  unit_confidence?: string
  /** 单位推断的正证与反证（照接口原文展示，不加工）。 */
  unit_evidence?: string
  unit_basis?: string
  series?: { date: string; scale_yi: number | null; net_sub_yi: number | null }[]
  sample_count?: number
  latest?: { date: string; scale_yi: number | null; net_sub_yi: number | null }
  /** 最新值在**同一读源样本**内的分位；不是托底日分位（无任何阈值筛选）。 */
  latest_percentile_in_samples?: number | null
  max_net_sub_yi?: number | null
  min_net_sub_yi?: number | null
  single_share_supported?: boolean
  single_share_note?: string
  note?: string
}
type DailyResp = {
  state: string
  dates?: string[]
  total_amount_yi?: (number | null)[]
  net_sub_yi?: (number | null)[]
  by_category?: { category: string; amount_yi: (number | null)[] }[]
  /** 每个数列自己的单位：成交额亿元、SC8 份额亿份（推断）。 */
  units?: { total_amount?: string; net_sub?: string; scale?: string }
  note?: string
  source?: string
  sources?: string[]
  source_labels?: string[]
  source_by_date?: Record<string, string[]>
  source_note?: string
}

/** 来源标识 → 页面文案。未登记标识直接显示原值，不猜成 TQ。 */
const SOURCE_STATE_LABEL: Record<string, string> = {
  tdx_tq: '通达信 TQ',
  em_trends2: '东财 trends2',
  mixed: '混合来源',
  unknown: '来源未知',
  no_data: '无数据',
}

function sourceText(resp?: { source?: string; source_labels?: string[] } | null): string {
  const labels = resp?.source_labels || []
  if (labels.length) return labels.join(' / ')
  return SOURCE_STATE_LABEL[resp?.source || ''] || resp?.source || '来源未知'
}

const STATE_LABEL: Record<string, { text: string; cls: string }> = {
  surge: { text: '显著放量', cls: 'text-role-risk' },
  active: { text: '偏活跃', cls: 'text-role-watch' },
  normal: { text: '常态', cls: 'text-foreground' },
  quiet: { text: '偏清淡', cls: 'text-muted-foreground' },
  insufficient: { text: '数据不全・不给结论', cls: 'text-muted-foreground' },
  no_baseline: { text: '基线不足・不给结论', cls: 'text-muted-foreground' },
  no_data: { text: '无数据', cls: 'text-muted-foreground' },
  ok: { text: '可用', cls: 'text-foreground' },
}

/** 不给结论的原因码 → 页面文案（与后端 `WITHHELD_LABELS` 一一对应）。 */
const WITHHELD_LABEL: Record<string, string> = {
  no_minutes: '没有可用于比较的分钟（无累计可算）',
  minutes_without_amount: '有分钟金额全是 NULL（未落库，不补 0）',
  minutes_short_basket: '有分钟篮子缺只（未达全篮子 20/20）',
  minute_gaps: '已采集分钟之间有缺口（缺一分钟就让累计少一截）',
  coverage_below_min: '齐备分钟占已观测分钟的比例不足 80%',
  baseline_no_sample: '基线没有「同日全篮子 + 同一批分钟」的可比交易日',
  baseline_zero: '基线同期累计为 0（除不出比值）',
}

const PHASE_LABEL: Record<string, string> = {
  pre: '盘前',
  live: '盘中',
  closed: '已收盘',
  lunch: '午间休市',
  non_trading_day: '非交易日',
}

/** 数字一律走 safe*（全仓纪律：null → `--`，不抛不编）。 */
function fmt(v: number | null | undefined, digits = 2): string {
  return safeFixed(v, digits)
}

/** 带正负号的净额（净流入语义交给 safeNetInflow，避免自己拼 `+`）。 */
function signed(v: number | null | undefined, digits = 2): string {
  return safeNetInflow(v, digits)
}

/** 双线迷你图：今日累计 vs 同期基线。只有 ≥2 个点才画（1 个点画成线是撒谎）。
 *
 * 2026-11-07 修: ARIA/图例原来写死「过去 20 日」「最近 20 个有数据交易日」—— 实际可能只有
 * 2 个有效基线日，这属于虚报样本量。基线天数一律按 payload 的真实值展示，最多 20 日。 */
function CumChart({
  series,
  baselineDays,
  baselineRequested,
}: {
  series: IntradayPoint[]
  baselineDays?: number | null
  baselineRequested?: number | null
}) {
  const req = baselineRequested ?? 20
  const baseDesc =
    baselineDays === null || baselineDays === undefined
      ? '同期基线（样本交易日数未披露，最多 20 日）'
      : `同期基线（实际 ${baselineDays}/${req} 个交易日${baselineDays >= req ? '' : '，样本不足 20 日'}）`
  const usable = series.filter(p => p.cum_yi !== null)
  if (usable.length < 2) {
    return <div className="flex h-[220px] items-center justify-center text-[14px] text-muted-foreground">数据点不足（需要至少 2 分钟）</div>
  }
  const w = 720
  const h = 220
  const pad = { l: 8, r: 8, t: 10, b: 18 }
  const maxY = Math.max(
    ...usable.map(p => Math.max(p.cum_yi, p.baseline_cum_yi ?? 0)),
    0.0001,
  )
  const x = (i: number) => pad.l + (i / Math.max(1, usable.length - 1)) * (w - pad.l - pad.r)
  const y = (v: number) => h - pad.b - (v / maxY) * (h - pad.t - pad.b)
  const round1 = (n: number) => Math.round(n * 10) / 10
  const todayPath = usable.map((p, i) => `${i === 0 ? 'M' : 'L'}${round1(x(i))},${round1(y(p.cum_yi))}`).join(' ')
  const basePath = usable
    .map((p, i) => (p.baseline_cum_yi === null ? null : `${i === 0 ? 'M' : 'L'}${round1(x(i))},${round1(y(p.baseline_cum_yi))}`))
    .filter(Boolean)
    .join(' ')

  return (
    <div className="w-full overflow-hidden">
      <svg viewBox={`0 0 ${w} ${h}`} className="h-[220px] w-full" role="img" aria-label={`今日累计成交额与同期基线对比（${baseDesc.replace('同期基线（', '').replace(/）$/, '')}）`}>
        <path d={basePath} fill="none" stroke="var(--muted-foreground)" strokeWidth="1.5" strokeDasharray="4 3" opacity="0.7" />
        <path d={todayPath} fill="none" stroke="var(--primary)" strokeWidth="2" />
        <line x1={pad.l} y1={h - pad.b} x2={w - pad.r} y2={h - pad.b} stroke="var(--border)" strokeWidth="1" />
      </svg>
      <div className="mt-2 flex flex-wrap items-center gap-4 text-[12px] text-muted-foreground">
        <span className="flex items-center gap-1"><span className="inline-block h-[2px] w-5 bg-primary" />今日累计</span>
        <span className="flex items-center gap-1"><span className="inline-block h-[2px] w-5 border-t border-dashed border-muted-foreground" />{baseDesc}</span>
        <span>纵轴上限 {safeFixed(maxY, 1)} 亿元</span>
      </div>
    </div>
  )
}

function Stat({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: 'up' | 'down' | 'neutral' }) {
  return (
    <div className="rounded-lg bg-s2 px-3 py-2.5">
      <div className="text-[12px] text-muted-foreground">{label}</div>
      <div
        className={cn(
          'mt-1 font-num text-[16px] font-semibold tabular-nums',
          tone === 'up' && 'text-stock-up',
          tone === 'down' && 'text-stock-down',
        )}
      >
        {value}
      </div>
      {hint ? <div className="mt-0.5 text-[12px] text-muted-foreground">{hint}</div> : null}
    </div>
  )
}

function Section({ title, desc, children, extra }: { title: string; desc: string; children: React.ReactNode; extra?: React.ReactNode }) {
  return (
    <section className="rounded-xl bg-card p-4 sm:p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-[16px] font-semibold tracking-tight">{title}</h2>
          <p className="mt-1 max-w-3xl text-[12px] leading-6 text-muted-foreground">{desc}</p>
        </div>
        {extra}
      </div>
      <div className="mt-4">{children}</div>
    </section>
  )
}

export default function EtfWatchPage() {
  const [intraday, setIntraday] = useState<IntradayResp | null>(null)
  const [support, setSupport] = useState<SupportResp | null>(null)
  const [daily, setDaily] = useState<DailyResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [dailyDays, setDailyDays] = useState(20)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [i, s, d] = await Promise.all([
        fetchAPI<IntradayResp>('/etf-watch/intraday', { cacheMode: false }),
        fetchAPI<SupportResp>('/etf-watch/support', { cacheMode: false }),
        fetchAPI<DailyResp>(`/etf-watch/daily?days=${dailyDays}`, { cacheMode: false }),
      ])
      setIntraday(i)
      setSupport(s)
      setDaily(d)
    } catch (e) {
      setError((e as Error)?.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }, [dailyDays])

  useEffect(() => {
    void load()
  }, [load])

  const totals = intraday?.totals
  const stateMeta = STATE_LABEL[intraday?.state || 'no_data'] || STATE_LABEL.no_data
  const ratio = totals?.ratio_vs_baseline ?? null
  const ratioTone: 'up' | 'down' | 'neutral' = ratio === null ? 'neutral' : ratio >= 1.15 ? 'up' : ratio <= 0.7 ? 'down' : 'neutral'

  const supportRows = useMemo(() => (support?.series || []).slice(-12).reverse(), [support])
  const dailyDates = daily?.dates || []
  /** SC8 单位由接口给（推断为亿份）。 */
  const shareUnit = support?.net_sub_unit || support?.unit || '亿份'
  /** 单位是推断（不是已确认事实）：页面必须把这个词摆出来。 */
  const unitInferred = (support?.unit_confidence || 'inferred') === 'inferred'
  const supportLag = support?.freshness?.lag_trading_days ?? null
  const intradaySource = sourceText(intraday)
  const intradayMixed = intraday?.source === 'mixed'
  const dailySource = sourceText(daily)
  // 逐日来源明细只在窗口较小的时候展开（120 日窗口全展开会把脚注挤爆）。
  const dailySourceEntries = Object.entries(daily?.source_by_date || {})
  const dailySourceDetail =
    dailySourceEntries.length && dailySourceEntries.length <= 8
      ? `；逐日 ${dailySourceEntries.map(([d, labels]) => `${d}=${labels.join('/')}`).join('、')}`
      : ''

  return (
    <div className="page-container">
      <PageHeader
        title="国家队 ETF"
        description="20 只重点宽基 ETF 的分钟成交额与市场级 SC8 份额（单位推断为亿份）。用于观察宽基 ETF 的成交与份额变化，不构成投资建议。"
        metadata={
          <>
            <span>分钟来源·{intradaySource}{intradayMixed ? '（混源）' : ''}</span>
            <span>{intraday?.date ? `${intraday.date.slice(0, 4)}-${intraday.date.slice(4, 6)}-${intraday.date.slice(6, 8)}` : '--'}</span>
            <span>当前时段 {PHASE_LABEL[intraday?.session_phase || intraday?.phase || ''] || '--'}</span>
          </>
        }
        actions={
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading}
            className="inline-flex min-h-9 items-center gap-1.5 rounded-lg px-3 text-[14px] text-foreground hover:bg-s2 disabled:opacity-50"
            aria-label="刷新国家队 ETF 数据"
          >
            <RefreshCw className={cn('h-4 w-4', loading && 'animate-spin')} aria-hidden="true" />
            刷新
          </button>
        }
      />

      {error ? (
        <div role="alert" className="mb-4 flex items-start gap-2 rounded-lg bg-role-risk/10 px-3 py-2 text-[14px] text-role-risk">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <span>{error}（数据未加载成功，页面不展示任何推测值）</span>
        </div>
      ) : null}

      {loading && !intraday ? (
        <div className="flex items-center gap-2 rounded-xl bg-card p-6 text-[14px] text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> 加载中…
        </div>
      ) : (
        <div className="space-y-4">
          {/* ① 盘中分钟观测 */}
          <Section
            title="盘中分钟观测"
            desc="20 只重点宽基 ETF 每分钟成交额加总。国家队进场常先体现在宽基 ETF 放量，而不是个股涨停；基线只取最多 20 个交易日内「同日 20/20 只齐备、金额非空、且覆盖本次同一批分钟」的日子，按「同一分钟集合的累计」取日均（比的是同日同期，不是全天）——16/20 只的日子合计天然少 4 只的钱，不算基线样本。缺只、金额 NULL、缺分钟或缺的日期，一律不给活跃度结论，只报覆盖。"
            extra={
              <span className={cn('rounded-full bg-s2 px-2 py-0.5 text-[12px] font-medium', stateMeta.cls)}>
                {stateMeta.text}
              </span>
            }
          >
            {intraday?.state === 'no_data' ? (
              <div className="flex items-start gap-2 rounded-lg bg-s2 px-3 py-3 text-[14px] text-muted-foreground">
                <Info className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
                <span>{intraday.note || '当日无分钟数据。'}</span>
              </div>
            ) : (
              <>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  <Stat label="今日累计成交额" value={`${fmt(totals?.cum_yi)} 亿元`} hint={totals?.last_minute ? `截至 ${totals.last_minute.slice(0, 2)}:${totals.last_minute.slice(2)}（已采集）` : undefined} />
                  <Stat label="同期基线均值" value={`${fmt(totals?.baseline_avg_yi)} 亿元`} hint={`基线 ${totals?.baseline_days ?? 0}/${totals?.baseline_days_requested ?? 20} 个交易日（20/20 齐备·同一批分钟）`} />
                  <Stat label="相对基线" value={ratio === null ? '--' : `${fmt(ratio)} 倍`} tone={ratioTone} hint={ratio === null ? '基线不足或数据不全，不外推' : ratio >= 1.15 ? '放量' : ratio <= 0.7 ? '清淡' : '接近常态'} />
                  <Stat label="已采到分钟" value={`${totals?.minutes ?? 0} 分钟`} hint={`20/20 只齐备 ${totals?.complete_minutes ?? totals?.full_minutes ?? 0} 分钟`} />
                </div>

                <div
                  className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg bg-s2 px-3 py-2 text-[12px] leading-6 text-muted-foreground"
                  role="status"
                  aria-label="分钟数据来源"
                >
                  <span>分钟数据来源：{intradaySource}{intradayMixed ? '（混源，逐日不同）' : ''}</span>
                  {intraday?.source_note ? <span>· {intraday.source_note}</span> : null}
                  {intraday?.baseline_sources?.some(s => s !== 'tdx_tq') ? (
                    <span className="text-role-watch">· 基线含非 TQ / 未标注来源，比值只作参考</span>
                  ) : null}
                </div>

                <div className="mt-4">
                  <CumChart
                    series={intraday?.series || []}
                    baselineDays={totals?.baseline_days ?? null}
                    baselineRequested={totals?.baseline_days_requested ?? 20}
                  />
                </div>

                {intraday?.fallback_from ? (
                  <div className="mt-3 flex items-start gap-2 rounded-lg bg-role-watch/10 px-3 py-2 text-[12px] leading-6 text-role-watch">
                    <Info className="mt-1 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                    <span>
                      今日（{intraday.fallback_from}）的分钟数据尚未就绪，当前显示最近交易日 {intraday.date} 的完整数据。
                      这不是今天的行情（数据日 {intraday.date} 为{PHASE_LABEL[intraday.phase] || '未知'}，当前时段 {PHASE_LABEL[intraday.session_phase || ''] || '未知'}）。
                    </span>
                  </div>
                ) : null}

                {intraday?.state === 'insufficient' ? (
                  <div className="mt-3 space-y-1 rounded-lg bg-role-watch/10 px-3 py-2 text-[12px] leading-6 text-role-watch">
                    <p className="font-medium">数据不全，本次不给活跃度结论（不拿残缺/不同口径的样本互相比较）：</p>
                    {(totals?.withheld_reasons || []).length ? (
                      <ul className="list-disc pl-4">
                        {(totals?.withheld_reasons || []).map(code => (
                          <li key={code}>{WITHHELD_LABEL[code] || code}</li>
                        ))}
                      </ul>
                    ) : null}
                    <p>
                      已采集 {totals?.minutes ?? 0} 分钟 / 20 只里 {totals?.complete_minutes ?? totals?.full_minutes ?? 0} 分钟齐备
                      （{totals?.coverage === null || totals?.coverage === undefined ? '--' : `${Math.round(totals.coverage * 1000) / 10}%`}）
                      {(totals?.minutes_short_basket ?? 0) ? `，${totals?.minutes_short_basket} 分钟篮子缺只` : ''}
                      {(totals?.minutes_without_amount ?? 0) ? `，${totals?.minutes_without_amount} 分钟金额全 NULL` : ''}
                      {(totals?.minute_gaps ?? 0) ? `，缺 ${totals?.minute_gaps} 根分钟（${(totals?.minute_gaps_detail || []).join('/') || '明细未取到'}）` : ''}
                      ；已采集合计 {fmt(totals?.cum_yi)} 亿元只代表采到的部分，不是全天成交额。
                    </p>
                  </div>
                ) : null}
                {intraday?.state === 'no_baseline' ? (
                  <div className="mt-3 space-y-1 rounded-lg bg-s2 px-3 py-2 text-[12px] leading-6 text-muted-foreground">
                    <p className="font-medium text-foreground">无同期基线可比，本次不给活跃度结论：</p>
                    <p>
                      基线只收「同日 20/20 只齐备 + 覆盖同一批 {totals?.baseline_minutes_compared ?? 0} 根分钟」的交易日，
                      本次可用 {totals?.baseline_days ?? 0}/{totals?.baseline_days_requested ?? 20} 日
                      {(totals?.baseline_days_incomplete_basket ?? 0) ? `，${totals?.baseline_days_incomplete_basket} 日因篮子缺只被剔除` : ''}
                      {(totals?.baseline_days_minute_gap ?? 0) ? `，${totals?.baseline_days_minute_gap} 日因缺分钟被剔除` : ''}
                      {(totals?.baseline_missing_days ?? 0) ? `，窗口内缺 ${totals?.baseline_missing_days} 个交易日样本（未采集还是非交易日未确认）` : ''}
                      。
                    </p>
                  </div>
                ) : null}
                {totals?.in_progress_minute ? (
                  <div className="mt-3 rounded-lg bg-s2 px-3 py-2 text-[12px] leading-6 text-muted-foreground">
                    {totals.in_progress_minute.slice(0, 2)}:{totals.in_progress_minute.slice(2)} 这根分钟仍在成形，未计入累计与结论（盘中实时观测，不因此判数据不全）。
                  </div>
                ) : null}
              </>
            )}
          </Section>

          {/* ② 份额信号（SC8，单位推断） */}
          <Section
            title="ETF 份额信号（SC8）"
            desc={`市场级 SC8 两列推断为全市场 ETF 总份额与当日份额变动（单位推断为 ${shareUnit}，不是金额）。没有官方字段定义、也没有可换算的金额阈值，因此本页不按任何阈值判定「托底日」，只展示原始样本与样本内分位。`}
          >
            {support?.state === 'no_data' ? (
              <div className="rounded-lg bg-s2 px-3 py-3 text-[14px] text-muted-foreground">{support.note || '暂无市场级 SC8 份额数据。'}</div>
            ) : (
              <>
                {support?.stale || support?.freshness?.stale ? (
                  <div className="mb-3 flex items-start gap-2 rounded-lg bg-role-watch/10 px-3 py-2 text-[12px] leading-6 text-role-watch" role="status">
                    <AlertTriangle className="mt-1 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                    <span>
                      数据停在 {support?.as_of || support?.latest?.date || '--'}（
                      {supportLag === null ? '滞后天数无法确认' : `已滞后 ${supportLag} 个交易日`}
                      ），上游 SC8 市场统计序列未更新。以下为历史份额，不是当前读数，不能当作今日份额或份额变动。
                    </span>
                  </div>
                ) : null}
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  <Stat label="最新交易日份额变动" value={signed(support?.latest?.net_sub_yi)} tone={(support?.latest?.net_sub_yi ?? 0) >= 0 ? 'up' : 'down'} hint={`${support?.latest?.date ?? '--'}（${shareUnit}）`} />
                  <Stat label="全市场 ETF 总份额" value={fmt(support?.latest?.scale_yi)} hint={`${shareUnit}（推断口径）`} />
                  <Stat label="样本交易日数" value={`${support?.sample_count ?? 0} 天`} hint="读源原样，未做阈值筛选" />
                  <Stat
                    label="最新值在样本内分位"
                    value={support?.latest_percentile_in_samples === null || support?.latest_percentile_in_samples === undefined ? '--' : `${support.latest_percentile_in_samples}%`}
                    hint="仅描述在同一读源样本里的排名，不是托底判定"
                  />
                </div>

                {unitInferred ? (
                  <div className="mt-3 rounded-lg bg-s2 px-3 py-2 text-[12px] leading-6 text-muted-foreground" role="note">
                    <span className="font-medium text-foreground">单位口径为推断（inferred），不是已确认事实。</span>
                    {support?.unit_evidence ? ` ${support.unit_evidence}` : ' 正证是交易所份额合计对拍 -0.07%，反证是同口径 Σ份额×净值 为列0 的 1.42 倍。'}
                  </div>
                ) : null}

                {supportRows.length ? (
                  <div className="mt-4 overflow-x-auto">
                    <table className="w-full min-w-[520px] text-[14px]">
                      <thead>
                        <tr className="text-left text-[12px] text-muted-foreground">
                          <th className="py-1.5 font-normal">日期</th>
                          <th className="py-1.5 text-right font-normal">份额变动（{shareUnit}）</th>
                          <th className="py-1.5 text-right font-normal">总份额（{shareUnit}）</th>
                        </tr>
                      </thead>
                      <tbody>
                        {supportRows.map(row => {
                          return (
                            <tr key={row.date} className="border-t border-border">
                              <td className="py-1.5 font-num tabular-nums">{row.date}</td>
                              <td className={cn('py-1.5 text-right font-num tabular-nums', (row.net_sub_yi ?? 0) >= 0 ? 'text-stock-up' : 'text-stock-down')}>
                                {signed(row.net_sub_yi)}
                              </td>
                              <td className="py-1.5 text-right font-num tabular-nums">{fmt(row.scale_yi)}</td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                    <p className="mt-2 text-[12px] leading-6 text-muted-foreground">最近 12 个样本日（不是按阈值筛出的托底日）。份额变动只是份额口径，不等于金额净流入。</p>
                  </div>
                ) : null}

                {support?.single_share_supported === false ? (
                  <div className="mt-3 flex items-start gap-2 rounded-lg bg-s2 px-3 py-2 text-[12px] leading-6 text-muted-foreground">
                    <Info className="mt-1 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                    <span>{support.single_share_note}</span>
                  </div>
                ) : null}
              </>
            )}
          </Section>

          {/* ③ 日度全景 */}
          <Section
            title="日度资金全景"
            desc="按跟踪指数分 12 类看每天的钱去了哪一类。分类成交额能看出是「全面放量」还是「只在沪深300」——单日总量看不出这个区别。"
            extra={
              <div className="flex items-center gap-1 rounded-lg bg-s2 p-0.5">
                {[10, 20, 40].map(n => (
                  <button
                    key={n}
                    type="button"
                    onClick={() => setDailyDays(n)}
                    className={cn(
                      'min-h-8 rounded-md px-2.5 text-[12px]',
                      dailyDays === n ? 'bg-card text-foreground' : 'text-muted-foreground hover:text-foreground',
                    )}
                    aria-pressed={dailyDays === n}
                  >
                    近 {n} 日
                  </button>
                ))}
              </div>
            }
          >
            {daily?.state !== 'ok' || !dailyDates.length ? (
              <div className="rounded-lg bg-s2 px-3 py-3 text-[14px] text-muted-foreground">{daily?.note || '暂无日度数据。'}</div>
            ) : (
              <>
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[640px] text-[14px]">
                    <thead>
                      <tr className="text-left text-[12px] text-muted-foreground">
                        <th className="sticky left-0 bg-card py-1.5 font-normal">分类</th>
                        {dailyDates.map(d => (
                          <th key={d} className="py-1.5 text-right font-normal font-num tabular-nums">{d.slice(4, 6)}-{d.slice(6, 8)}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      <tr className="border-t border-border">
                        <td className="sticky left-0 bg-card py-1.5">合计成交额</td>
                        {(daily.total_amount_yi || []).map((v, i) => (
                          <td key={i} className="py-1.5 text-right font-num tabular-nums">{fmt(v, 1)}</td>
                        ))}
                      </tr>
                      <tr className="border-t border-border">
                        <td className="sticky left-0 bg-card py-1.5">市场级份额变动</td>
                        {(daily.net_sub_yi || []).map((v, i) => (
                          <td key={i} className={cn('py-1.5 text-right font-num tabular-nums', (v ?? 0) >= 0 ? 'text-stock-up' : 'text-stock-down')}>
                            {signed(v, 1)}
                          </td>
                        ))}
                      </tr>
                      {(daily.by_category || []).map(cat => (
                        <tr key={cat.category} className="border-t border-border">
                          <td className="sticky left-0 bg-card py-1.5">{cat.category}</td>
                          {cat.amount_yi.map((v, i) => (
                            <td key={i} className="py-1.5 text-right font-num tabular-nums text-muted-foreground">{fmt(v, 1)}</td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <p className="mt-2 text-[12px] leading-6 text-muted-foreground">
                  成交额单位亿元；市场级列为 SC8 份额口径（单位推断为亿份，是份额不是金额）。成交额按 20 只重点 ETF 归入 12 类；
                  份额变动只有市场级（全市场 ETF 合计），不能拆到单只或单类，也不能换算成金额净流入。
                </p>
                <p className="mt-1 text-[12px] leading-6 text-muted-foreground">
                  分钟数据来源（逐日）：{dailySource}
                  {daily?.source_note ? `；${daily.source_note}` : ''}
                  {dailySourceDetail}
                </p>
              </>
            )}
          </Section>

          <div className="rounded-xl bg-card p-4 text-[12px] leading-6 text-muted-foreground">
            <p>
              口径说明：分钟成交额按库内 <code>etf_minute_flow.source</code> 逐日标注真实来源
              （通达信 TQ / 东财 trends2 兜底 / 混合 / 未知 NULL 照实展示，不写死单一来源）；本次页面读到的来源为
              {intradaySource}{intradayMixed ? '（混源，比值只作参考）' : ''}。市场级数据来自 SC8 序列
              （推断为全市场 ETF 总份额与份额变动，单位推断为亿份；单位是推断而非已确认事实；
              该序列已停更，页面按库内真实日期展示并标滞后）。
              单只 ETF 的份额在通达信是当前值回填，因此**不提供单只净申购**；需要拆到单只时须另接份额数据源。
              本页只陈述资金行为，不构成任何投资建议。
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
