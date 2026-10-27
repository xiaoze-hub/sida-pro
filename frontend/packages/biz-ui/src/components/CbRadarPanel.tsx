import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { RefreshCw, Link2, ShieldAlert, TrendingDown, Radio } from 'lucide-react'
import { fetchAPI } from '@sida/api'
import { Button } from '@sida/base-ui/components/ui/button'
import { safeFixed } from '@/lib/format'
import CaliberBadge from '@sida/biz-ui/components/CaliberBadge'

/**
 * 转债 - 正股联动套利雷达(I-4 初版, 2026-10-18)
 *
 * 数据 GET /api/cb-radar/snapshot?limit=N (fetchAPI 自动补 /api 前缀)。
 *
 * 口径(后端 src/core/cb_radar.py):
 *   转股价值 = 100 / 转股价 × 正股现价
 *   溢价率   = 转债价 / 转股价值 - 1
 *   双低值   = 转债价 + 溢价率×100
 *   强赎距离 = (正股价 - 强赎触发价) / 强赎触发价
 *   纯债溢价 = 转债价 / 纯债价值 - 1
 * 转股价/强赎触发价/纯债价值均为**通达信客户端原值**, 未自行贴现或估算; 缺字段显 '—'。
 *
 * 数据日/交易阶段由后端给(A-03 铁律): 非竞价/非交易时段显示"数据日 + 快照",
 * **绝不用浏览器时钟冒充实时同步**。
 */

export interface CbScenario {
  key: 'stock_limit_up_lag' | 'discount_arb' | 'force_redeem_breached' | 'force_redeem_near' | 'double_low_top'
  label: string
  tone: 'opportunity' | 'risk' | 'watch' | 'info'
  detail: string
}

export interface CbRow {
  code: string
  name: string
  stock_code: string
  stock_name?: string
  rating?: string | null
  bond_price?: number | null
  bond_chg_pct?: number | null
  conv_price?: number | null
  conv_value?: number | null
  conv_chg_pct?: number | null
  premium_pct?: number | null
  double_low?: number | null
  double_low_rank?: number | null
  stock_price?: number | null
  stock_chg_pct?: number | null
  lag_pct?: number | null
  force_redeem_price?: number | null
  redeem_distance_pct?: number | null
  pure_bond_value?: number | null
  pure_bond_premium_pct?: number | null
  end_date?: string | null
  scenarios?: CbScenario[]
  scenario_keys?: string[]
}

export interface CbRadarResp {
  available: boolean
  data_date?: string | null
  trade_phase?: string
  phase_label?: string
  is_live?: boolean
  is_today?: boolean
  as_of?: string
  source?: string
  caliber?: string
  universe?: number
  evaluated?: number
  rows?: CbRow[]
  scenario_counts?: Record<string, number>
  note?: string | null
  cached?: boolean
  age_s?: number
}

interface CbRadarPanelProps {
  limit?: number
  /** 点击行跳正股工作台(宿主注入) */
  onOpenStock?: (stockCode: string, name?: string) => void
}

type FilterKey = 'all' | 'actionable' | 'discount_arb' | 'force_redeem' | 'stock_limit_up_lag' | 'double_low_top'

const FILTERS: { key: FilterKey; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'actionable', label: '有场景' },
  { key: 'discount_arb', label: '折价' },
  { key: 'force_redeem', label: '强赎' },
  { key: 'stock_limit_up_lag', label: '正股封板' },
  { key: 'double_low_top', label: '双低前列' },
]

function fmtNum(v: number | null | undefined, digits = 2): string {
  return typeof v === 'number' && Number.isFinite(v) ? safeFixed(v, digits) : '—'
}

function fmtPct(v: number | null | undefined): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—'
  return `${v > 0 ? '+' : ''}${safeFixed(v, 2)}%`
}

function toneClass(tone: CbScenario['tone']): string {
  switch (tone) {
    case 'opportunity':
      return 'bg-red-500/15 text-red-500 border-red-500/25'
    case 'risk':
      return 'bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/25'
    case 'watch':
      return 'bg-yellow-500/15 text-yellow-600 dark:text-yellow-400 border-yellow-500/25'
    default:
      return 'bg-muted text-muted-foreground border-border'
  }
}

/** 溢价率染色: 折价(≤-1%)才是套利空间, 用涨色; 其余中性。 */
function premiumClass(v: number | null | undefined): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return 'text-muted-foreground'
  return v <= -1 ? 'text-stock-up font-semibold' : 'text-foreground'
}

/** 强赎距离: ≥0 = 正股已在触发价上方(风险), 用琥珀; -10~0 = 接近。 */
function redeemClass(v: number | null | undefined): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return 'text-muted-foreground'
  if (v >= 0) return 'text-amber-600 dark:text-amber-400 font-semibold'
  if (v >= -10) return 'text-yellow-600 dark:text-yellow-400'
  return 'text-muted-foreground'
}

export default function CbRadarPanel({ limit = 60, onOpenStock }: CbRadarPanelProps) {
  const [data, setData] = useState<CbRadarResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [filter, setFilter] = useState<FilterKey>('all')
  const mountedRef = useRef(true)

  const load = useCallback(
    async (refresh = false) => {
      setError('')
      try {
        const q = `/cb-radar/snapshot?limit=${limit}${refresh ? '&refresh=1' : ''}`
        const res = await fetchAPI<CbRadarResp>(q, { cacheMode: 'reload' })
        if (mountedRef.current) setData(res)
      } catch (e) {
        if (mountedRef.current) setError(e instanceof Error ? e.message : '转债雷达加载失败')
      } finally {
        if (mountedRef.current) setLoading(false)
      }
    },
    [limit]
  )

  useEffect(() => {
    mountedRef.current = true
    void load()
    // 转债条款盘后不变, 60s 轮询只在盘中才有意义(后端也会按阶段调 TTL)
    const t = window.setInterval(() => {
      if (document.visibilityState === 'visible') void load()
    }, 60000)
    return () => {
      mountedRef.current = false
      window.clearInterval(t)
    }
  }, [load])

  const rows = useMemo(() => data?.rows ?? [], [data?.rows])
  const counts = data?.scenario_counts ?? {}

  const shown = useMemo(() => {
    if (filter === 'all') return rows
    if (filter === 'actionable') return rows.filter((r) => (r.scenarios?.length ?? 0) > 0)
    if (filter === 'force_redeem') {
      return rows.filter((r) => r.scenario_keys?.some((k) => k.startsWith('force_redeem')))
    }
    return rows.filter((r) => r.scenario_keys?.includes(filter))
  }, [rows, filter])

  const asOfTime = (() => {
    if (!data?.as_of) return '--'
    const s = data.as_of
    return s.length >= 19 ? s.slice(11, 19) : s
  })()

  return (
    <div className="space-y-3">
      {/* 顶栏: 数据日 / 交易阶段 / 是否实时 + 刷新 */}
      <div className="card-surface flex flex-wrap items-center justify-between gap-2 rounded-lg bg-card px-3 py-2">
        <div className="flex flex-wrap items-center gap-2 text-[12px] text-muted-foreground">
          <Link2 className="h-3.5 w-3.5 text-primary" />
          {data?.is_live ? (
            <span>
              转债雷达 · <span className="font-semibold text-stock-up">实时</span>
              <span className="font-mono text-foreground"> {asOfTime}</span>
            </span>
          ) : (
            <span>
              转债雷达 · 数据日 <span className="font-mono text-foreground">{data?.data_date ?? '--'}</span>
              <span className="ml-1 rounded bg-muted px-1.5 py-0.5 text-[12px]">
                {data?.phase_label ?? '未知阶段'}
                {data?.is_today ? ' · 今日已定型' : ' · 历史快照'}
              </span>
            </span>
          )}
          <span className="opacity-40">|</span>
          <span>
            评估 <span className="font-mono text-foreground">{data?.evaluated ?? 0}</span> / 全池{' '}
            <span className="font-mono text-foreground">{data?.universe ?? 0}</span> 只
          </span>
          <CaliberBadge
            caliber="unknown"
            label="TQ 条款"
            title="通达信 TQ: get_kzz_info 条款(转股价/强赎触发价/纯债价值, 客户端原值) + get_pricevol 批量行情"
            className="text-[12px]"
          />
          {data?.cached ? <span className="text-[12px] opacity-70">缓存 {data.age_s ?? 0}s</span> : null}
        </div>
        <Button
          variant="secondary"
          size="sm"
          className="h-7 text-[12px]"
          onClick={() => {
            setLoading(true)
            void load(true)
          }}
          disabled={loading}
        >
          <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} /> 刷新
        </Button>
      </div>

      {/* 场景计数条 */}
      <div className="flex flex-wrap gap-2">
        <div className="card-surface flex items-center gap-1.5 rounded-lg bg-card px-2.5 py-1.5 text-[12px]">
          <TrendingDown className="h-3.5 w-3.5 text-stock-up" />
          <span className="text-muted-foreground">折价套利</span>
          <span className="font-mono font-semibold text-foreground">{counts.discount_arb ?? 0}</span>
        </div>
        <div className="card-surface flex items-center gap-1.5 rounded-lg bg-card px-2.5 py-1.5 text-[12px]">
          <ShieldAlert className="h-3.5 w-3.5 text-amber-500" />
          <span className="text-muted-foreground">强赎触发区</span>
          <span className="font-mono font-semibold text-foreground">{counts.force_redeem_breached ?? 0}</span>
        </div>
        <div className="card-surface flex items-center gap-1.5 rounded-lg bg-card px-2.5 py-1.5 text-[12px]">
          <Radio className="h-3.5 w-3.5 text-primary" />
          <span className="text-muted-foreground">正股封板·转债滞后</span>
          <span className="font-mono font-semibold text-foreground">{counts.stock_limit_up_lag ?? 0}</span>
        </div>
        <div className="card-surface flex items-center gap-1.5 rounded-lg bg-card px-2.5 py-1.5 text-[12px]">
          <span className="text-muted-foreground">双低前列</span>
          <span className="font-mono font-semibold text-foreground">{counts.double_low_top ?? 0}</span>
        </div>
      </div>

      {/* 过滤器 */}
      <div className="flex flex-wrap gap-1.5">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            type="button"
            onClick={() => setFilter(f.key)}
            className={`rounded-md border px-2 py-1 text-[12px] transition-colors ${
              filter === f.key
                ? 'border-primary/40 bg-primary/10 text-primary'
                : 'border-border bg-card text-muted-foreground hover:text-foreground'
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {error ? <div className="text-[12px] text-amber-600 dark:text-amber-500">{error}</div> : null}
      {data && !data.available ? (
        <div className="card-surface rounded-lg bg-card px-3 py-6 text-center text-[12px] text-muted-foreground">
          {data.note || '转债数据暂不可用'}
        </div>
      ) : null}

      {data?.available ? (
        <div className="card-surface bg-card overflow-x-auto rounded-lg ">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="border-b border-border text-[12px] text-muted-foreground">
                <th className="px-2 py-2 text-left font-medium">转债 / 正股</th>
                <th className="px-2 py-2 text-right font-medium">债价</th>
                <th className="px-2 py-2 text-right font-medium">债涨幅</th>
                <th className="px-2 py-2 text-right font-medium">转股价值</th>
                <th className="px-2 py-2 text-right font-medium" title="转债价 / 转股价值 - 1">
                  溢价率
                </th>
                <th className="px-2 py-2 text-right font-medium" title="转债价 + 溢价率×100">
                  双低值
                </th>
                <th className="px-2 py-2 text-right font-medium" title="(正股价 - 强赎触发价) / 强赎触发价">
                  强赎距离
                </th>
                <th className="px-2 py-2 text-right font-medium" title="转债价 / 纯债价值 - 1">
                  纯债溢价
                </th>
                <th className="px-2 py-2 text-left font-medium">场景</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.code} className="border-b border-border hover:bg-s2">
                  <td className="px-2 py-1.5">
                    <button
                      type="button"
                      className="text-left"
                      onClick={() => r.stock_code && onOpenStock?.(r.stock_code, r.stock_name || r.name)}
                      title={r.stock_code ? `查看正股 ${r.stock_code}` : ''}
                    >
                      <div className="flex items-center gap-1.5">
                        <span className="font-medium text-foreground">{r.name}</span>
                        {r.rating ? (
                          <span className="rounded bg-muted px-1 py-0.5 text-[12px] text-muted-foreground">{r.rating}</span>
                        ) : null}
                      </div>
                      <div className="font-mono text-[12px] text-muted-foreground">
                        {r.code} · 正股 {r.stock_code || '--'}
                      </div>
                    </button>
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums text-foreground">{fmtNum(r.bond_price)}</td>
                  <td
                    className={`px-2 py-1.5 text-right font-mono tabular-nums ${
                      (r.bond_chg_pct ?? 0) > 0 ? 'text-stock-up' : (r.bond_chg_pct ?? 0) < 0 ? 'text-stock-down' : 'text-muted-foreground'
                    }`}
                  >
                    {fmtPct(r.bond_chg_pct)}
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums text-foreground">{fmtNum(r.conv_value)}</td>
                  <td className={`px-2 py-1.5 text-right font-mono tabular-nums ${premiumClass(r.premium_pct)}`}>
                    {fmtPct(r.premium_pct)}
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums text-foreground">
                    {fmtNum(r.double_low)}
                    {r.double_low_rank ? (
                      <span className="ml-1 text-[12px] text-muted-foreground">#{r.double_low_rank}</span>
                    ) : null}
                  </td>
                  <td className={`px-2 py-1.5 text-right font-mono tabular-nums ${redeemClass(r.redeem_distance_pct)}`}>
                    {fmtPct(r.redeem_distance_pct)}
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono tabular-nums text-muted-foreground">
                    {fmtPct(r.pure_bond_premium_pct)}
                  </td>
                  <td className="px-2 py-1.5">
                    <div className="flex flex-wrap gap-1">
                      {(r.scenarios ?? []).map((s) => (
                        <span
                          key={s.key}
                          className={`rounded border px-1.5 py-0.5 text-[12px] font-medium ${toneClass(s.tone)}`}
                          title={s.detail}
                        >
                          {s.label}
                        </span>
                      ))}
                      {(r.scenarios?.length ?? 0) === 0 ? <span className="text-[12px] text-muted-foreground">常态</span> : null}
                    </div>
                  </td>
                </tr>
              ))}
              {shown.length === 0 && !loading ? (
                <tr>
                  <td colSpan={9} className="py-8 text-center text-[12px] text-muted-foreground">
                    当前筛选下无转债（共评估 {rows.length} 只）
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      ) : null}

      {loading && rows.length === 0 ? (
        <div className="card-surface h-[140px] animate-pulse rounded-lg bg-s2" />
      ) : null}

      {data?.available ? (
        <p className="text-[12px] leading-relaxed text-muted-foreground">
          口径: {data.caliber || '—'}。数据源: {data.source || '—'}。折价率与强赎距离均为事实计算，未计交易费用、T+1
          与强赎公告不确定性，不构成投资建议。
        </p>
      ) : null}
    </div>
  )
}
