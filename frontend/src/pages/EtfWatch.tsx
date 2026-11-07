/**
 * 国家队 ETF 看板（2026-11-05）—— 对标「用 Tushare 复刻国家队看板」那套口径，
 * 数据源换成**通达信 TQ 本地网关**（零配额、零外部依赖）。
 *
 * ## 三层，各自的"为什么看"
 * 1. **盘中分钟观测**：20 只重点宽基 ETF 每分钟成交额加总，和上证综指放一起看。
 *    意义：国家队进场往往先体现在**宽基 ETF 的成交额突然放大**，而不是个股涨停。
 *    基线取「过去 20 个交易日同一分钟」的累计均值 —— 比的是同日同期，不是全天，
 *    否则早盘永远"清淡"、尾盘永远"放量"，结论全是假的。
 * 2. **托底信号**：市场级 ETF 规模与当日净申购（SC8）。净申购 = 份额变动 × 净值，
 *    单日 ≥100 亿视为托底日样本；盘中先看成交额在历史托底日里的分位。
 * 3. **日度全景**：20 只 ETF 按跟踪指数分 12 类的成交额。
 *
 * ## 口径纪律（不许违反）
 * - **单只 ETF 净申购不存在**：通达信 ETF 份额是"当前值回填到每一天"（实测 63 个交易日
 *   零变化），算出来恒为 0。所以只给市场级净申购，页面上必须显式说明，不能用别的算法凑。
 * - 数据缺失一律显示 `--` + 原因，不写 0、不猜。
 * - 单位：成交额亿元；净申购亿元。数字一律 tabular-nums。
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
    cum_yi: number
    baseline_avg_yi: number | null
    ratio_vs_baseline: number | null
    baseline_days: number
    baseline_days_requested?: number
    baseline_missing_days?: number
    baseline_confirmed?: boolean
    last_minute: string | null
    etfs_expected: number
    /** 过 ≥80% 闸门（20 只篮子 = ≥16 只）的分钟数。 */
    full_minutes: number
    /** 真·20 只全齐的分钟数（与 full_minutes 不同，别混）。 */
    strict_minutes?: number
    min_etfs_for_complete?: number
    minutes: number
  }
  benchmark?: { code: string; name: string; points: { minute: string; close: number }[] }
  note?: string
}
type SupportResp = {
  state: string
  threshold_yi?: number
  series?: { date: string; scale_yi: number | null; net_sub_yi: number | null }[]
  support_days?: { date: string; net_sub_yi: number | null }[]
  support_count?: number
  sample_count?: number
  latest?: { date: string; scale_yi: number | null; net_sub_yi: number | null }
  latest_percentile_in_support_days?: number | null
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
  note?: string
}

const STATE_LABEL: Record<string, { text: string; cls: string }> = {
  surge: { text: '显著放量', cls: 'text-role-risk' },
  active: { text: '偏活跃', cls: 'text-role-watch' },
  normal: { text: '常态', cls: 'text-foreground' },
  quiet: { text: '偏清淡', cls: 'text-muted-foreground' },
  insufficient: { text: '数据不全', cls: 'text-muted-foreground' },
  no_baseline: { text: '基线不足', cls: 'text-muted-foreground' },
  no_data: { text: '无数据', cls: 'text-muted-foreground' },
  ok: { text: '可用', cls: 'text-foreground' },
}

const PHASE_LABEL: Record<string, string> = {
  pre: '盘前',
  live: '盘中',
  closed: '已收盘',
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

  const supportRows = useMemo(() => (support?.support_days || []).slice(-12).reverse(), [support])
  const dailyDates = daily?.dates || []

  return (
    <div className="page-container">
      <PageHeader
        title="国家队 ETF"
        description="20 只重点宽基 ETF 的分钟成交额与市场级净申购。用于判断托底资金是否在场，不构成投资建议。"
        metadata={
          <>
            <span>数据源·通达信 TQ</span>
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
            desc="20 只重点宽基 ETF 每分钟成交额加总。国家队进场常先体现在宽基 ETF 放量，而不是个股涨停；基线取最多 20 个有数据交易日「同一分钟」的累计均值，比的是同日同期；实际参与的样本交易日数按图例/字段标注，不足 20 日不做等同处理。"
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
                  <Stat label="今日累计成交额" value={`${fmt(totals?.cum_yi)} 亿`} hint={totals?.last_minute ? `截至 ${totals.last_minute.slice(0, 2)}:${totals.last_minute.slice(2)}` : undefined} />
                  <Stat label="同期基线均值" value={`${fmt(totals?.baseline_avg_yi)} 亿`} hint={`基线 ${totals?.baseline_days ?? 0}/${totals?.baseline_days_requested ?? 20} 个交易日`} />
                  <Stat label="相对基线" value={ratio === null ? '--' : `${fmt(ratio)} 倍`} tone={ratioTone} hint={ratio === null ? '基线不足，不外推' : ratio >= 1.15 ? '放量' : ratio <= 0.7 ? '清淡' : '接近常态'} />
                  <Stat label="已采到分钟" value={`${totals?.minutes ?? 0} 分钟`} hint={`≥${totals?.min_etfs_for_complete ?? 16} 只 ${totals?.full_minutes ?? 0} 分钟`} />
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
                  <div className="mt-3 rounded-lg bg-role-watch/10 px-3 py-2 text-[12px] text-role-watch">
                    当日多数分钟未达 ≥16/20（80%）覆盖闸门，活跃度结论不可用（半场数据会把成交额算少一截）。
                  </div>
                ) : null}
              </>
            )}
          </Section>

          {/* ② 托底信号 */}
          <Section
            title="ETF 托底信号"
            desc={`净申购 = 当日 ETF 份额变动 × 净值，单日 ≥${support?.threshold_yi ?? 100} 亿元视为托底日样本。盘中先看成交额在历史托底日里的位置，等份额公布后再看净申购强度。`}
          >
            {support?.state === 'no_data' ? (
              <div className="rounded-lg bg-s2 px-3 py-3 text-[14px] text-muted-foreground">{support.note || '暂无市场级净申购数据。'}</div>
            ) : (
              <>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  <Stat label="最新交易日净申购" value={`${signed(support?.latest?.net_sub_yi)} 亿`} tone={(support?.latest?.net_sub_yi ?? 0) >= 0 ? 'up' : 'down'} hint={support?.latest?.date} />
                  <Stat label="全市场 ETF 规模" value={`${fmt(support?.latest?.scale_yi)} 亿`} />
                  <Stat label="托底日样本数" value={`${support?.support_count ?? 0} 天`} hint={`样本区间 ${support?.sample_count ?? 0} 个交易日`} />
                  <Stat
                    label="最新值在托底样本中的分位"
                    value={support?.latest_percentile_in_support_days === null || support?.latest_percentile_in_support_days === undefined ? '--' : `${support.latest_percentile_in_support_days}%`}
                    hint="越高说明这次净流入在历史托底日里越靠前"
                  />
                </div>

                {supportRows.length ? (
                  <div className="mt-4 overflow-x-auto">
                    <table className="w-full min-w-[520px] text-[14px]">
                      <thead>
                        <tr className="text-left text-[12px] text-muted-foreground">
                          <th className="py-1.5 font-normal">日期</th>
                          <th className="py-1.5 text-right font-normal">净申购（亿元）</th>
                          <th className="py-1.5 text-right font-normal">ETF 规模（亿元）</th>
                        </tr>
                      </thead>
                      <tbody>
                        {supportRows.map(row => {
                          const scale = support?.series?.find(x => x.date === row.date)?.scale_yi ?? null
                          return (
                            <tr key={row.date} className="border-t border-border">
                              <td className="py-1.5 font-num tabular-nums">{row.date}</td>
                              <td className={cn('py-1.5 text-right font-num tabular-nums', (row.net_sub_yi ?? 0) >= 0 ? 'text-stock-up' : 'text-stock-down')}>
                                {signed(row.net_sub_yi)}
                              </td>
                              <td className="py-1.5 text-right font-num tabular-nums">{fmt(scale)}</td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                    <p className="mt-2 text-[12px] leading-6 text-muted-foreground">最近 12 个托底日。托底日密集出现（连续多日 ≥{support?.threshold_yi ?? 100} 亿）时，说明资金是持续进场而非单日脉冲。</p>
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
            desc="按跟踪指数分 12 类看每天的钱去了哪一类。分类成交额能看出是「全面托底」还是「只护沪深300」——单日总量看不出这个区别。"
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
                        <td className="sticky left-0 bg-card py-1.5">市场级净申购</td>
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
                  单位亿元。成交额按 20 只重点 ETF 归入 12 类；净申购只有市场级（全市场 ETF 合计），不能拆到单只或单类。
                </p>
              </>
            )}
          </Section>

          <div className="rounded-xl bg-card p-4 text-[12px] leading-6 text-muted-foreground">
            <p>
              口径说明：分钟成交额来自通达信 TQ 网关（一次批量调用取 20 只，非逐只请求）；净申购来自市场级 SC8 序列。
              单只 ETF 的份额在通达信是当前值回填，因此**不提供单只净申购**；需要拆到单只时须另接份额数据源。
              本页只陈述资金行为，不构成任何投资建议。
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
