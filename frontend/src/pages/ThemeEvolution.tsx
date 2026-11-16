import { useCallback, useEffect, useMemo, useState } from 'react'
import { fetchAPI } from '@sida/api'
import { RefreshCw } from 'lucide-react'
import PageHeader from '@/components/PageHeader'
import Sparkline from '@/components/Sparkline'
import { Button } from '@sida/base-ui/components/ui/button'

/**
 * 题材演化观察层(2026-11-16): 只读证据页。
 *
 * 与"题材情绪"页的区别(别混): 那个是服务端打分的排行; 这一页只展示**采集到的原始证据** ——
 * 每个涨停股当日被源标了哪些概念/行业、批次覆盖到哪、哪天缺采。没有打分、没有主线判断、
 * 没有买卖建议。
 *
 * 三条口径必须显示在页面上(后端字段保证, 前端不许美化):
 * 1. `captured_at` 是**我们的采集时刻**, 不是供应商发布时间;
 * 2. 题材归属是**采集时点**的当前成员关系(`block_as_of`), 不是目标日的历史成分;
 * 3. `status=partial` 表示覆盖不完整 —— 不是失败, 也不是完整快照。
 */

interface RunPayload {
  run_id: number
  day: string
  attempt_seq: number
  status: 'complete' | 'partial' | 'refused' | string
  input_partial: boolean
  source: string
  captured_at: string | null
  captured_at_is_vendor_publish_time: boolean
  source_fresh: boolean | null
  source_trade_date: string | null
  boards_total: number
  boards_ok: number
  boards_empty_ok: number
  boards_fetch_failed: number
  pool_state: string | null
  limit_up_count: number | null
  capture_notes: string[]
}

interface DaysResp {
  days: RunPayload[]
  count: number
  notes: string[]
}

interface ThemeStock {
  stock_code: string
  stock_name: string | null
}

interface ThemeBucket {
  kind: 'industry' | 'concept'
  name: string
  limit_up_count: number
  stocks: ThemeStock[]
  block_as_of: string[]
}

interface DayResp {
  day: string
  run: RunPayload
  themes: ThemeBucket[]
  labels: {
    basis: string
    block_as_of_values: string[]
    stocks_without_labels: number
    is_historical_membership: boolean
    note: string
  }
  coverage: Record<string, unknown>
}

interface TimelineResp {
  days: string[]
  themes: Array<{
    kind: 'industry' | 'concept'
    name: string
    counts: number[]
    total: number
    max: number
    days_present: number
  }>
  notes: string[]
}

interface ClosesResp {
  items: Array<{ symbol: string; closes: number[]; asof: string; source: string }>
  missing: string[]
}

const STATUS_LABEL: Record<string, string> = {
  complete: '覆盖完整（源自报）',
  partial: '部分覆盖',
  refused: '已拒绝',
}

function statusTone(status: string): string {
  if (status === 'complete') return 'text-emerald-600'
  if (status === 'partial') return 'text-amber-600'
  return 'text-red-500'
}

/** 单元格底色档位: 用设计 token 的 s1..s4, 不手调透明度。 */
function cellTone(count: number, max: number): string {
  if (count <= 0 || max <= 0) return ''
  const ratio = count / max
  if (ratio > 0.75) return 'bg-s4'
  if (ratio > 0.5) return 'bg-s3'
  if (ratio > 0.25) return 'bg-s2'
  return 'bg-s1'
}

function fmtDay(day: string): string {
  return day.length === 8 ? `${day.slice(4, 6)}-${day.slice(6, 8)}` : day
}

export default function ThemeEvolution() {
  const [days, setDays] = useState<RunPayload[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<DayResp | null>(null)
  const [timeline, setTimeline] = useState<TimelineResp | null>(null)
  const [activeTheme, setActiveTheme] = useState<string | null>(null)
  const [closes, setCloses] = useState<Record<string, number[]>>({})
  const [missingCloses, setMissingCloses] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const loadDays = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [d, t] = await Promise.all([
        fetchAPI<DaysResp>('/theme-evolution/days?limit=60'),
        fetchAPI<TimelineResp>('/theme-evolution/timeline?days=20'),
      ])
      setDays(d.days || [])
      setTimeline(t)
      setSelected((prev) => prev ?? d.days?.[0]?.day ?? null)
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadDays()
  }, [loadDays])

  useEffect(() => {
    if (!selected) return
    let alive = true
    setDetail(null)
    fetchAPI<DayResp>(`/theme-evolution/day/${selected}`)
      .then((res) => {
        if (alive) setDetail(res)
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : '该日数据加载失败')
      })
    return () => {
      alive = false
    }
  }, [selected])

  const theme = useMemo(
    () => detail?.themes.find((t) => `${t.kind}:${t.name}` === activeTheme) ?? null,
    [detail, activeTheme],
  )

  // 只在选中某个题材时, 拉这一组股票的收盘序列(≤20 只), 不整页乱发请求。
  useEffect(() => {
    if (!theme) {
      setCloses({})
      setMissingCloses([])
      return
    }
    const symbols = theme.stocks.slice(0, 20).map((s) => s.stock_code)
    if (symbols.length === 0) return
    let alive = true
    fetchAPI<ClosesResp>(`/klines/closes?symbols=${encodeURIComponent(symbols.join(','))}&days=20`)
      .then((res) => {
        if (!alive) return
        const map: Record<string, number[]> = {}
        for (const item of res.items || []) map[item.symbol] = item.closes
        setCloses(map)
        setMissingCloses(res.missing || [])
      })
      .catch(() => {
        if (!alive) return
        setCloses({})
        setMissingCloses(symbols)
      })
    return () => {
      alive = false
    }
  }, [theme])

  const maxCell = useMemo(
    () => Math.max(1, ...(timeline?.themes ?? []).map((t) => t.max)),
    [timeline],
  )

  return (
    <div className="page-container space-y-4">
      <PageHeader
        title="题材演化观察"
        description="采集时刻的原始证据：每个涨停股当日被标了哪些概念、批次覆盖到哪、哪几天缺采。只读，不预测方向。"
        actions={
          <Button variant="ghost" size="sm" onClick={() => void loadDays()} disabled={loading}>
            <RefreshCw className={loading ? 'animate-spin' : ''} size={14} />
            刷新
          </Button>
        }
        metadata={
          detail && (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <span>
                批次 <span className={statusTone(detail.run.status)}>{STATUS_LABEL[detail.run.status] ?? detail.run.status}</span>
              </span>
              <span>
                板块 {detail.run.boards_ok}/{detail.run.boards_total} 取到
                {detail.run.boards_fetch_failed > 0 && ` · ${detail.run.boards_fetch_failed} 失败`}
              </span>
              <span>涨停 {detail.run.limit_up_count ?? '未取到'}</span>
              <span>采集于 {detail.run.captured_at ?? '未知'}（非供应商发布时间）</span>
              {detail.run.source_trade_date && <span>源交易日 {detail.run.source_trade_date}</span>}
            </div>
          )
        }
      />

      {error && (
        <div className="mb-3 text-[14px] text-red-500" role="alert">
          {error}
        </div>
      )}

      {/* 日期条: 每个有批次的日子一格, 缺采的日子不会出现(这里不是交易日历) */}
      <div className="mb-4 flex gap-1 overflow-x-auto pb-1">
        {days.map((d) => (
          <button
            key={d.day}
            type="button"
            onClick={() => {
              setSelected(d.day)
              setActiveTheme(null)
            }}
            className={[
              'shrink-0 rounded-md px-2 py-1 text-left text-xs tabular-nums transition-colors',
              selected === d.day ? 'bg-accent text-accent-foreground' : 'hover:bg-s2',
            ].join(' ')}
          >
            <div>{fmtDay(d.day)}</div>
            <div className="text-xs text-muted-foreground">
              {d.limit_up_count ?? '—'} 只 · {d.status === 'complete' ? '全' : '半'}
            </div>
          </button>
        ))}
        {!loading && days.length === 0 && (
          <div className="text-[14px] text-muted-foreground">
            还没有采集批次。默认关闭：设置 <code>SIDA_THEME_EVOLUTION_AGENT=1</code> 后在工作日 16:10 自动采集。
          </div>
        )}
      </div>

      {/* 演化矩阵: 近 20 个"有批次"的日子 × 题材的涨停家数 */}
      {timeline && timeline.themes.length > 0 && (
        <section className="mb-6">
          <h2 className="mb-2 text-[14px] font-medium">
            近 {timeline.days.length} 个采集日的题材家数
            <span className="ml-2 text-xs font-normal text-muted-foreground">
              榜内同现计数，不是全市场成分覆盖
            </span>
          </h2>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-xs tabular-nums">
              <thead>
                <tr className="border-b border-border text-muted-foreground">
                  <th className="sticky left-0 bg-background py-1 pr-3 text-left font-normal">题材</th>
                  {timeline.days.map((d) => (
                    <th key={d} className="px-1 py-1 text-right font-normal">
                      {fmtDay(d)}
                    </th>
                  ))}
                  <th className="px-2 py-1 text-right font-normal">合计</th>
                </tr>
              </thead>
              <tbody>
                {timeline.themes.slice(0, 30).map((t) => (
                  <tr key={`${t.kind}:${t.name}`} className="border-b border-border">
                    <td className="sticky left-0 bg-background py-1 pr-3">
                      <span className="mr-1 text-xs text-muted-foreground">
                        {t.kind === 'industry' ? '行业' : '概念'}
                      </span>
                      {t.name}
                    </td>
                    {t.counts.map((c, i) => (
                      <td key={timeline.days[i]} className="relative px-1 py-1 text-right">
                        {c > 0 && (
                          <span
                            aria-hidden
                            className={`absolute inset-x-0.5 inset-y-0.5 rounded-[2px] ${cellTone(c, maxCell)}`}
                          />
                        )}
                        <span className="relative">{c || ''}</span>
                      </td>
                    ))}
                    <td className="px-2 py-1 text-right font-medium">{t.total}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {/* 当日题材 → 个股 */}
      {detail && (
        <section>
          <h2 className="mb-2 text-[14px] font-medium">
            {detail.day} 的题材
            <span className="ml-2 text-xs font-normal text-muted-foreground">
              {detail.labels.is_historical_membership === false &&
                '归属为采集时点的当前成员关系，非历史成分'}
            </span>
          </h2>
          {detail.themes.length === 0 ? (
            <div className="text-[14px] text-muted-foreground">
              该批次没有可聚合的题材归属
              {detail.labels.stocks_without_labels > 0 &&
                `（${detail.labels.stocks_without_labels} 只涨停股没有归属字段）`}
            </div>
          ) : (
            <ul className="divide-y divide-border">
              {detail.themes.map((t) => {
                const key = `${t.kind}:${t.name}`
                const open = activeTheme === key
                return (
                  <li key={key}>
                    <button
                      type="button"
                      onClick={() => setActiveTheme(open ? null : key)}
                      className="flex w-full items-baseline justify-between gap-3 py-2 text-left hover:bg-s1"
                    >
                      <span className="flex items-baseline gap-2">
                        <span className="text-xs uppercase tracking-wide text-muted-foreground">
                          {t.kind === 'industry' ? '行业' : '概念'}
                        </span>
                        <span className="text-[14px]">{t.name}</span>
                      </span>
                      <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                        {t.limit_up_count} 只
                      </span>
                    </button>
                    {open && (
                      <ul className="mb-2 ml-1 border-l border-border pl-3">
                        {t.stocks.map((s) => {
                          const series = closes[s.stock_code]
                          return (
                            <li
                              key={s.stock_code}
                              className="flex items-center justify-between gap-3 py-1 text-xs"
                            >
                              <span className="tabular-nums">
                                {s.stock_code}
                                <span className="ml-2 text-muted-foreground">{s.stock_name ?? '—'}</span>
                              </span>
                              <span className="w-24 shrink-0">
                                {series ? (
                                  <Sparkline data={series} height={18} stroke="currentColor" />
                                ) : (
                                  <span className="text-xs text-muted-foreground">无收盘序列</span>
                                )}
                              </span>
                            </li>
                          )
                        })}
                        {missingCloses.length > 0 && (
                          <li className="py-1 text-xs text-muted-foreground">
                            {missingCloses.length} 只在库里没有足够收盘序列（不补零、不画平线）
                          </li>
                        )}
                      </ul>
                    )}
                  </li>
                )
              })}
            </ul>
          )}

          <div className="mt-4 space-y-1 border-t border-border pt-3 text-xs text-muted-foreground">
            <div>{detail.labels.note}</div>
            <div>
              归属反查日 {detail.labels.block_as_of_values.join('、') || '未知'}
              {detail.labels.stocks_without_labels > 0 &&
                ` · ${detail.labels.stocks_without_labels} 只无归属`}
            </div>
            <div>
              批次口径：{STATUS_LABEL[detail.run.status] ?? detail.run.status}；采集时刻不等于数据定稿时刻。
            </div>
          </div>
        </section>
      )}
    </div>
  )
}
