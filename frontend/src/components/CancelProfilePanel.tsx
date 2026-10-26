import { safeFixed, safeNum } from '@/lib/format'

/**
 * I-3 委托行为画像时序带（纯展示组件, 不取数、不重算口径）。
 *
 * 为什么单独一个文件: L2Tab 是取数容器(InsightProvider + 30s 轮询), 而这块的所有数字
 * 都由后端 `/api/market/cancel-profile/{symbol}` 算好(中位数/标准差/分位/z/结论)。
 * 前端**只渲染**, 不再自己算一遍——两处算法必然分叉, 且分位口径一旦不一致就会互相打脸。
 *
 * 视觉: 60 日撤单率柱带 + 常态中位数虚线 + 今日红点; 一句话结论在上方。
 * 缺值(date 有、撤单率 null)画成 2px 灰条并在 title 里写明"缺值", 不画成 0
 * (0 = 真的没撤单, 是有效信息, 不能与"没数据"混为一谈)。
 */

export interface CancelProfileDay {
  date: string
  b_rate: number | null
  s_rate?: number | null
  b_cancel?: number | null
  s_cancel?: number | null
  b_order?: number | null
  s_order?: number | null
}

export interface CancelProfileSide {
  rate?: number | null
  median: number | null
  std?: number | null
  p90?: number | null
  min?: number | null
  max?: number | null
  n: number
  z: number | null
  percentile: number | null
}

export interface CancelProfileVerdict {
  code: string
  label: string
  level: 'safe' | 'caution' | 'warning' | 'unknown' | string
  abnormal: boolean | null
  desc: string
}

export interface CancelProfile {
  available: boolean
  reason?: string | null
  series?: CancelProfileDay[]
  today?: CancelProfileDay | null
  buy?: CancelProfileSide | null
  sell?: CancelProfileSide | null
  verdict?: CancelProfileVerdict | null
  conclusion?: string | null
  as_of?: string | null
  window?: number
  baseline_days?: number
  min_baseline_days?: number
  caliber?: string
}

/** 撤单率(0~1) → 百分比文本; 缺值走 safeFixed 的 '--'。 */
export function rateText(v: unknown, digits = 1): string {
  const n = safeNum(v)
  return n == null ? '--' : `${safeFixed(n * 100, digits)}%`
}

export function verdictClass(level?: string | null): string {
  if (level === 'warning') return 'text-amber-500'
  if (level === 'caution') return 'text-amber-600/90 dark:text-amber-400/90'
  if (level === 'unknown') return 'text-muted-foreground'
  return 'text-muted-foreground'
}

function barTitle(d: CancelProfileDay): string {
  const rate = safeNum(d.b_rate)
  return `${d.date} · ${rate == null ? '撤单率缺值（不按 0 计）' : `买撤 ${rateText(rate)}`}`
}

/**
 * 60 日撤单率柱带。高度按**窗口内最大值**归一（分位/中位数才是判定依据, 绝对高度只是形状）。
 * 今日 = 序列最后一根(与后端 `as_of` 同源), 用琥珀色标出。
 */
export function CancelProfileBand({ profile }: { profile: CancelProfile }) {
  const series = (profile.series ?? []).filter((d) => !!d.date)
  const buy = profile.buy
  const median = safeNum(buy?.median)
  const scale = Math.max(
    safeNum(buy?.max) ?? 0,
    ...series.map((d) => safeNum(d.b_rate) ?? 0),
    0.01,
  )
  const medianPct = median == null ? null : Math.min(100, (median / scale) * 100)
  const lastIdx = series.length - 1

  return (
    <div data-testid="cancel-profile-band" className="space-y-1">
      <div className="relative h-16 w-full border-b border-border">
        {medianPct != null ? (
          <div
            data-testid="cancel-profile-median-line"
            className="pointer-events-none absolute inset-x-0 border-t border-dashed border-foreground/40"
            style={{ bottom: `${medianPct}%` }}
            title={`常态中位数 ${rateText(median)}`}
          />
        ) : null}
        <div className="flex h-full w-full items-end gap-px">
          {series.map((d, i) => {
            const rate = safeNum(d.b_rate)
            const isToday = i === lastIdx
            const h = rate == null ? 2 : Math.max(2, Math.min(100, (rate / scale) * 100))
            return (
              <div
                key={`${d.date}-${i}`}
                data-testid={isToday ? 'cancel-profile-bar-today' : 'cancel-profile-bar'}
                data-date={d.date}
                className={`min-w-0 flex-1 ${isToday ? 'bg-amber-500' : 'bg-foreground/25'}`}
                style={rate == null ? { height: '2px', opacity: 0.35 } : { height: `${h}%` }}
                title={barTitle(d)}
              />
            )
          })}
        </div>
      </div>
      <div className="flex justify-between font-mono text-[12px] text-muted-foreground">
        <span>{series[0]?.date ?? ''}</span>
        <span>
          {series.length} 日 · 归一上限 {rateText(scale)}
        </span>
        <span>{series[lastIdx]?.date ?? ''}</span>
      </div>
    </div>
  )
}

/**
 * 一句话结论 + 基线读数。结论文本来自后端（含"PXX / 常态 X%±Ypp / z"），前端只加颜色。
 */
export default function CancelProfilePanel({
  profile,
  loading,
  failed,
  lastOkAt,
}: {
  profile: CancelProfile | null
  loading: boolean
  failed: boolean
  lastOkAt: string
}) {
  if (!profile) {
    return (
      <div data-testid="cancel-profile-empty" className="text-[12px] text-muted-foreground">
        {loading ? '加载中…' : failed ? `取数失败（${lastOkAt ? `上次成功 ${lastOkAt}` : '暂无成功记录'}）` : '暂无数据'}
      </div>
    )
  }

  if (!profile.available) {
    return (
      <div data-testid="cancel-profile-unavailable" className="space-y-1">
        <div className="text-[12px] text-amber-600 dark:text-amber-400">
          {profile.reason || '撤单基线不可用（不冒充 0）'}
        </div>
        {(profile.series?.length ?? 0) > 0 ? <CancelProfileBand profile={profile} /> : null}
      </div>
    )
  }

  const buy = profile.buy
  const sell = profile.sell
  const verdict = profile.verdict
  const z = safeNum(buy?.z)
  const pctile = safeNum(buy?.percentile)
  const stdPp = safeNum(buy?.std)
  const median = safeNum(buy?.median)

  return (
    <div className="space-y-2">
      <div
        data-testid="cancel-profile-conclusion"
        className={`text-[12px] leading-relaxed ${verdictClass(verdict?.level)}`}
      >
        {profile.conclusion || verdict?.desc || '--'}
      </div>
      <CancelProfileBand profile={profile} />
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[12px] md:grid-cols-4">
        <div title="今日买盘撤单率（BCancel/BOrder, 日线统计口径）">
          <div className="text-muted-foreground">今日买撤</div>
          <div className="font-mono text-foreground">{rateText(buy?.rate)}</div>
        </div>
        <div title={`今日在近 ${buy?.n ?? 0} 日基线中的分位（ECDF: 小于等于今日值的占比）`}>
          <div className="text-muted-foreground">历史分位</div>
          <div className="font-mono text-foreground">{pctile == null ? '--' : `P${safeFixed(pctile, 0)}`}</div>
        </div>
        <div title="该股近端常态撤单率中位数 ± 样本标准差（pp=百分点）">
          <div className="text-muted-foreground">常态中位数</div>
          <div className="font-mono text-foreground">
            {median == null ? '--' : `${rateText(median)}${stdPp == null ? '' : `±${safeFixed(stdPp * 100, 1)}pp`}`}
          </div>
        </div>
        <div title="(今日 - 中位数) / 标准差; |z|<1 为常态, z>2 且涨幅>3% 才判定相对异常诱多">
          <div className="text-muted-foreground">偏离 z</div>
          <div className={`font-mono ${verdictClass(verdict?.level)}`}>
            {z == null ? '--' : `${z > 0 ? '+' : ''}${safeFixed(z, 2)}`}
          </div>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-x-2 text-[12px] text-muted-foreground">
        <span>基线 {buy?.n ?? 0} 日</span>
        {sell?.z != null ? <span>· 卖撤 z {safeFixed(sell.z, 2)}</span> : null}
        {profile.as_of ? <span>· 数据日 {profile.as_of}</span> : null}
        {verdict?.label ? <span>· {verdict.label}</span> : null}
        {failed ? <span className="text-amber-500">· 本次刷新失败, 显示上次成功值</span> : null}
        {profile.caliber ? <span className="truncate">· {profile.caliber}</span> : null}
      </div>
    </div>
  )
}
