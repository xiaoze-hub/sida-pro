import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { fetchAPI } from '@panwatch/api'
import MarketMoodChart from '@/components/MarketMoodChart'
import LadderBoard, { type LadderDay } from '@panwatch/biz-ui/components/thememood/LadderBoard'
import { MarketPhasePanel } from '@/components/MarketPhasePanel'
import ScanJobButton from '@/components/ScanJobButton'
import {
  AXIS_CELL_W,
  AXIS_PITCH,
  axisDates,
  axisWidth,
  cellColorClass,
  cellTextClass,
  cellsByDate,
  dayLabels,
  fmtScore,
  monthBands,
  scoreTrend,
  type MoodCell,
  type TrendLine,
  type TrendDot,
} from '@/lib/theme-mood'

/** 全市场情绪温度序列(自算口径, 见后端 market_breadth_daily) */
interface MarketMoodResp {
  ok: boolean
  items: Array<{ date: string; up: number | null; down: number | null; flat: number | null; symbols: number | null; temperature: number | null }>
  latest?: { date: string; up: number | null; down: number | null; flat: number | null; symbols: number | null; temperature: number | null }
  temperature_percentile?: number | null
  note?: string
  caliber?: string
}

/**
 * 题材情绪页(2026-09-12, 老板口径): 收盘确认口径的题材×日情绪矩阵。
 * 口径见 docs/research/题材情绪分_设计方案_20260912.md; 数值来自 /api/theme-mood/board 落库读侧。
 */

interface CoreStock {
  symbol: string
  name: string | null
  boards: number | null
  pct: number | null
  score: number
  prob: number | null
}

interface MoodItem {
  block_code: string
  block_name: string | null
  block_type: string | null
  score: number | null
  delta: number | null
  confidence: number | null
  core: boolean
  s1: number | null
  s2: number | null
  s3: number | null
  s4: number | null
  s5: number | null
  limit_up_cnt: number | null
  max_boards: number | null
  core_stocks: CoreStock[]
  cells: MoodCell[]
  /** 轮动(v0.5.81): 窗口内进过每日 Top-K 的痕迹; 退榜题材仍留一行看它怎么退的 */
  top_days?: number
  first_top_date?: string | null
  last_top_date?: string | null
  in_top_today?: boolean
}

interface RotationDay {
  date: string
  new_n: number
  exit_n: number
  new_codes: string[]
  exit_codes: string[]
}

interface LadderResp {
  dates: string[]
  ladder: LadderDay[]
  note?: string
  mode?: 'live' | 'finalized'
  as_of?: string | null
  stale?: boolean
  degraded?: string | null
  live_day?: LadderDay | null
  note_closing?: string
  stats?: {
    prev_candidates: number
    first: number
    promoted: number
    blown: number
    broken: number
    charging: number
  } | null
}

interface BoardResp {
  trade_date: string | null
  window: number
  count: number
  dates?: string[]
  items: MoodItem[]
  market?: { date: string; score: number | null }[]
  rotation?: RotationDay[]
  rotation_top_k?: number
}

const WINDOWS = [10, 20, 30] as const
const DIMS = ['涨停结构', '题材扩散', '核心强度', '接力反馈', '连续性'] as const
const MARKET_CURVE_H = 46
const DETAIL_CURVE_H = 68

/** 轴长度变化时把滚动容器拉回"最新"一端(轮询刷新不打扰用户已滚动的位置)。 */
function useAutoScrollToLatest(axisLen: number) {
  const ref = useRef<HTMLDivElement | null>(null)
  const lastLen = useRef(0)
  useEffect(() => {
    const el = ref.current
    if (!el || axisLen === 0 || axisLen === lastLen.current) return
    lastLen.current = axisLen
    el.scrollLeft = el.scrollWidth
  }, [axisLen])
  return ref
}

/** 走势折线(与矩阵共用列几何): 面积 + 折线 + 逐点圆点, 最新点放大并描边。 */
function TrendChart({ trend, width, height, label, hint, ariaLabel }: {
  trend: TrendLine
  width: number
  height: number
  label?: ReactNode
  hint: (d: TrendDot) => string
  ariaLabel: string
}) {
  return (
    <div className="flex w-max min-w-full items-center gap-1">
      {label ? (
        <span className="sticky left-0 z-10 flex h-full w-[76px] shrink-0 flex-col justify-center bg-background px-1 leading-tight">
          {label}
        </span>
      ) : null}
      <svg width={width} height={height} className="block shrink-0" role="img" aria-label={ariaLabel}>
        {trend.areas.map((d, i) => (
          <path key={`a${i}`} d={d} className="fill-primary/10" />
        ))}
        {trend.lines.map((points, i) => (
          <polyline
            key={`l${i}`}
            points={points}
            className="fill-none stroke-primary"
            strokeWidth={1.6}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
        ))}
        {trend.dots.map((d) => (
          <circle key={d.i} cx={d.x} cy={d.y} r={1.8} className="fill-primary/60">
            <title>{hint(d)}</title>
          </circle>
        ))}
        {trend.last ? (
          <circle cx={trend.last.x} cy={trend.last.y} r={3.2} className="fill-primary stroke-background" strokeWidth={1.5} />
        ) : null}
      </svg>
    </div>
  )
}

export default function ThemeMoodPage() {
  const [windowDays, setWindowDays] = useState<number>(20)
  const [resp, setResp] = useState<BoardResp | null>(null)
  const [ladder, setLadder] = useState<LadderResp | null>(null)
  const [active, setActive] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  // 全市场情绪温度曲线(2026-09-25): 自算口径, 见后端 /api/market-breadth/history
  const [mood, setMood] = useState<MarketMoodResp | null>(null)
  useEffect(() => {
    let alive = true
    fetchAPI<MarketMoodResp>(`/market-data/breadth-history?days=${windowDays}`, { cacheMode: 'reload' })
      .then((r) => { if (alive) setMood(r) })
      .catch(() => { if (alive) setMood(null) })
    return () => { alive = false }
  }, [windowDays, reloadKey])
  const [showAllRows, setShowAllRows] = useState(false)
  // P1-1(2026-09-18): 右侧题材列表默认只给**主线 12 条**(分层第一层)。
  // 实测该列原先一次渲染 516 行且逐行画 1px 分隔线 → hairline 526、密度 9680, 是典型"表格墙"。
  const [showAllThemes, setShowAllThemes] = useState(false)
  const [boardCollapsed, setBoardCollapsed] = useState(
    () => localStorage.getItem('tm-board-collapsed') === '1',
  )

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const res = await fetchAPI<BoardResp>(`/theme-mood/board?window=${windowDays}&top=15`, { cacheMode: 'reload' })
        if (alive) setResp(res)
      } catch {
        /* 保留旧数据 */
      }
      try {
        const lad = await fetchAPI<LadderResp>(`/theme-mood/ladder?window=${windowDays}`, { cacheMode: 'reload' })
        if (alive) setLadder(lad)
      } catch {
        if (alive) setLadder(null)
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), 120000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [windowDays, reloadKey])

  // 盘中实时(v0.5.87): 梯队单独 60s 轮询(board 仍 120s); live_day/stale/note_closing 由 /ladder 带出
  useEffect(() => {
    let alive = true
    const poll = async () => {
      try {
        const lad = await fetchAPI<LadderResp>(`/theme-mood/ladder?window=${windowDays}`, {
          cacheMode: 'reload',
        })
        if (alive) setLadder(lad)
      } catch {
        /* 保留旧数据 */
      }
    }
    const timer = window.setInterval(() => void poll(), 60000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [windowDays, reloadKey])

  const items = resp?.items ?? []
  const rotationByDate = useMemo(
    () => new Map((resp?.rotation ?? []).map((r) => [r.date, r])),
    [resp],
  )
  const visibleItems = showAllRows ? items : items.slice(0, 15)
  /** 右列可见题材(默认主线 12 条; 展开后才是全量) */
  const visibleThemes = showAllThemes ? items : items.slice(0, 12)
  const axis = axisDates(resp?.dates, items[0]?.cells ?? [], windowDays)
  const bands = monthBands(axis)
  const labels = dayLabels(axis)
  const latestDate = axis.length ? axis[axis.length - 1] : null
  const detail = items.find((it) => it.block_code === active) ?? null
  const dims = detail ? [detail.s1, detail.s2, detail.s3, detail.s4, detail.s5] : []
  const axisW = axisWidth(axis.length)

  // 走势曲线: 顶部=强势题材(当日情绪分前20均值, 后端 market), 明细=选中题材自身情绪分。
  const marketByDate = new Map((resp?.market ?? []).map((m) => [m.date, m.score]))
  const marketTrend = axis.length ? scoreTrend(axis.map((d) => marketByDate.get(d) ?? null), { height: MARKET_CURVE_H }) : null
  const detailCells = detail ? cellsByDate(detail.cells) : null
  const detailTrend =
    detail && axis.length ? scoreTrend(axis.map((d) => detailCells?.get(d)?.score ?? null), { height: DETAIL_CURVE_H }) : null

  // 时间轴默认对齐"最新"一端; 仅轴长度变化(首次加载/切窗口)时回滚, 轮询刷新不动用户的滚动位置。
  const scrollRef = useAutoScrollToLatest(axis.length)
  const detailScrollRef = useAutoScrollToLatest(axis.length)

  return (
    <div className="mx-auto max-w-[1400px] p-4">
      <div className="mb-3 flex items-center gap-3">
        <h1 className="text-[16px] font-semibold">题材情绪</h1>
        <span className="rounded bg-accent px-1.5 py-0.5 text-[10px] text-muted-foreground">收盘确认口径</span>
        {resp?.trade_date ? <span className="text-[11px] text-muted-foreground">{resp.trade_date}</span> : null}
        <div className="ml-auto flex items-center gap-1">
          <ScanJobButton path="/theme-mood/scan/run" onDone={() => setReloadKey((k) => k + 1)} />
          {WINDOWS.map((w) => (
            <button
              key={w}
              type="button"
              onClick={() => setWindowDays(w)}
              className={`rounded px-2 py-0.5 text-[11px] ${
                windowDays === w ? 'bg-primary text-primary-foreground' : 'bg-accent/50 text-muted-foreground hover:bg-accent'
              }`}
            >
              {w}日
            </button>
          ))}
        </div>
      </div>

      <div className="mb-3">
        <MarketPhasePanel />
      </div>

      {mood?.ok ? (
        <div className="mb-3 border-b border-border/40 pb-2">
          <div className="mb-1 flex items-baseline gap-2">
            <span className="text-[11px] text-foreground/60">全市场情绪温度</span>
            <span className="font-mono text-[12px] font-medium text-primary">{mood.latest?.temperature ?? '—'}</span>
            <span className="text-[10px] text-muted-foreground">
              近 {mood.items?.length ?? 0} 日分位 {typeof mood.temperature_percentile === 'number' ? `${mood.temperature_percentile}%` : '—'}
            </span>
            <span className="ml-auto font-mono text-[10px] text-muted-foreground">
              涨 {mood.latest?.up ?? '—'} / 跌 {mood.latest?.down ?? '—'} / 平 {mood.latest?.flat ?? '—'}（{mood.latest?.symbols ?? '—'} 只有效）
            </span>
          </div>
          <MarketMoodChart
            data={(mood.items ?? []).map((i) => i.temperature).filter((v): v is number => typeof v === 'number')}
            width={1000}
            height={56}
            ariaLabel={`全市场情绪温度曲线, 最新 ${mood.latest?.temperature ?? '—'}`}
          />
          <div className="mt-0.5 text-[10px] text-muted-foreground">
            口径：{mood.caliber}{mood.note ? ` ⚠️ ${mood.note}` : ''}
          </div>
        </div>
      ) : null}

      <LadderBoard
        ladder={ladder?.ladder ?? []}
        liveDay={ladder?.live_day ?? null}
        mode={ladder?.mode ?? 'finalized'}
        stale={ladder?.stale ?? false}
        lastOk={null}
        noteClosing={ladder?.note_closing ?? null}
        stats={ladder?.stats ?? null}
        asOf={ladder?.as_of ?? null}
      />

      <button
        type="button"
        onClick={() =>
          setBoardCollapsed((v) => {
            const n = !v
            localStorage.setItem('tm-board-collapsed', n ? '1' : '0')
            return n
          })
        }
        className="mb-1 mt-3 w-full rounded border border-border/50 py-1 text-center text-[11px] text-muted-foreground"
      >
        {boardCollapsed ? '展开题材表' : '折叠题材表'}
      </button>

      {/* 走查 2026-09-18: 折叠只收**左侧题材列表**, 右侧题材×日期矩阵保持可见(原先整块 hidden) */}
      <div className="flex flex-col gap-3 xl:flex-row">
        {/* B3(2026-09-18): 这个题材列表是全市场板块(数百行), 没有限高时把整页撑到
            16456px(≈18 屏, 实测) —— 矩阵也被 flex 拉伸成同高。改为**列内滚动 + 粘性**:
            页面回到 2 屏左右, 列表自己滚。 */}
        <div
          className={`w-full shrink-0 xl:sticky xl:top-2 xl:max-h-[calc(100vh-160px)] xl:w-[420px] xl:overflow-y-auto ${
            boardCollapsed ? 'hidden xl:hidden' : ''
          }`}
        >
          <div className="sticky top-0 z-10 mb-1 grid grid-cols-[1fr_56px_56px_44px] gap-1 bg-background px-2 py-1 text-[10px] text-muted-foreground">
            <span>题材</span>
            <span className="text-right">情绪分</span>
            <span className="text-right">日变化</span>
            <span className="text-right">置信</span>
          </div>
          {/* 留白分隔取代逐行 1px 线(设计稿 v3.0 §七 6.3): 行线由 516 条降到 0 条, 层级交给分组与间距 */}
          <div className="rounded border border-border/60">
            {visibleThemes.map((it) => (
              <button
                key={it.block_code}
                type="button"
                onClick={() => setActive(it.block_code)}
                className={`grid w-full grid-cols-[1fr_56px_56px_44px] items-center gap-1 px-2 py-2 text-left text-[12px] hover:bg-accent/40 ${
                  active === it.block_code ? 'bg-accent/60' : ''
                }`}
              >
                <span className="truncate">
                  {it.block_name || it.block_code}
                  {it.core ? <span className="ml-1 rounded bg-stock-up/15 px-1 text-[10px] text-stock-up">核心</span> : null}
                </span>
                <span className={`text-right font-mono ${cellTextClass(it.score)}`}>{fmtScore(it.score)}</span>
                <span
                  className={`text-right font-mono text-[11px] ${
                    (it.delta ?? 0) >= 0 ? 'text-stock-up' : 'text-stock-down'
                  }`}
                >
                  {it.delta == null ? '--' : `${it.delta > 0 ? '+' : ''}${fmtScore(it.delta)}`}
                </span>
                <span className="text-right font-mono text-[11px] text-muted-foreground">{it.confidence ?? '--'}</span>
              </button>
            ))}
            {resp && resp.items.length === 0 ? (
              <div className="py-6 text-center text-[11px] text-muted-foreground">暂无题材情绪数据(等待盘后扫描)</div>
            ) : null}
            {items.length > 12 ? (
              <button
                type="button"
                onClick={() => setShowAllThemes((v) => !v)}
                className="w-full px-2 py-1.5 text-[11px] text-muted-foreground hover:text-foreground"
              >
                {showAllThemes ? '收起(只看主线 12 条)' : `展开全部 ${items.length} 条题材`}
              </button>
            ) : null}
          </div>
        </div>

        <div className="min-w-0 flex-1">
          <div className="rounded border border-border/60 p-2">
            <div className="mb-1 flex items-center gap-2 text-[11px] text-muted-foreground">
              <span>题材 × 日期(近 {windowDays} 个交易日 · 色块=情绪分)</span>
              {latestDate ? <span>最新 {latestDate}</span> : null}
              <span className="text-[10px]">曲线=当日前 20 均值</span>
              <span className="ml-auto flex items-center gap-1 text-[10px]">
                色阶
                {[
                  ['<50', 'bg-muted/40'],
                  ['50-60', 'bg-muted/60'],
                  ['60-70', 'bg-stock-up/15'],
                  ['70-82', 'bg-stock-up/30'],
                  ['≥82', 'bg-stock-up/45'],
                ].map(([label, cls]) => (
                  <span key={label} className="flex items-center gap-0.5">
                    <span className={`inline-block h-2.5 w-3.5 rounded-sm ${cls}`} />
                    {label}
                  </span>
                ))}
              </span>
            </div>
            {/* 横向滚动条跟随暗色主题(2026-09-14 走查: 矩阵默认浅色滚动条在暗底上刺眼)。
                .scrollbar 是 index.css 里的 token 化工具类(scrollbar-color: muted-foreground/0.35)。 */}
            <div ref={scrollRef} className="scrollbar overflow-x-auto pb-1">
              <div className="flex w-max min-w-full items-end gap-1">
                <span className="sticky left-0 z-10 w-[76px] shrink-0 bg-background" />
                <div className="flex gap-0.5">
                  {bands.map((b) => (
                    <span
                      key={b.key}
                      className="border-b border-border/70 pb-0.5 text-center text-[11px] font-medium leading-4 text-foreground/60"
                      style={{ width: b.count * AXIS_PITCH - (AXIS_PITCH - AXIS_CELL_W) }}
                    >
                      {b.label}
                    </span>
                  ))}
                </div>
              </div>
              <div className="mb-1 flex w-max min-w-full items-center gap-1">
                <span className="sticky left-0 z-10 w-[76px] shrink-0 bg-background text-[11px] leading-4 text-foreground/60">
                  日期
                </span>
                <div className="flex gap-0.5">
                  {axis.map((d, i) => (
                    <span
                      key={d}
                      title={`${d}${i === axis.length - 1 ? ' · 最新收盘' : ''}`}
                      className={`w-[38px] shrink-0 text-center text-[12px] leading-4 ${
                        i === axis.length - 1 ? 'font-semibold text-primary' : 'text-foreground/80'
                      }`}
                    >
                      {labels[i]}
                    </span>
                  ))}
                </div>
              </div>
              {/* 轮动行(2026-09-14 走查: 原先紧贴日期表头、只有 10px 小字, 读起来像表头的一部分)。
                  加分隔线 + 上间距, 标签升到 11px 并补 title 说明自身口径。 */}
              <div
                data-testid="thememood-rotation-row"
                className="mb-1 mt-2 flex w-max min-w-full items-center gap-1 border-t border-border/40 pt-1.5"
              >
                <span
                  title={`轮动: 每日新进 / 退出 Top${resp?.rotation_top_k ?? 10} 的题材数(绿色 = 新进, 划线 = 退出)`}
                  className="sticky left-0 z-10 w-[76px] shrink-0 bg-background text-[11px] leading-4 text-muted-foreground"
                >
                  轮动
                </span>
                <div className="flex gap-0.5">
                  {axis.map((d) => {
                    const r = rotationByDate.get(d)
                    const entered = r?.new_n ?? 0
                    const exited = r?.exit_n ?? 0
                    return (
                      <span
                        key={d}
                        title={r
                          ? `${d} 新进 Top${resp?.rotation_top_k ?? 10}: ${r.new_codes.join('、') || '无'}\n退榜: ${r.exit_codes.join('、') || '无'}`
                          : d}
                        className="w-[38px] shrink-0 text-center text-[11px] leading-4 text-foreground/60"
                      >
                        {entered || exited ? (
                          <>
                            {entered ? <span className="text-primary">+{entered}</span> : null}
                            {entered && exited ? ' ' : null}
                            {exited ? <span className="text-muted-foreground line-through">−{exited}</span> : null}
                          </>
                        ) : '·'}
                      </span>
                    )
                  })}
                </div>
              </div>
              {marketTrend ? (
                <div
                  className="mb-1 border-b border-border/40 pb-1"
                  title="情绪走势 = 当日情绪分前 20 名题材的均值(领先端), 不是全市场均值"
                >
                  <TrendChart
                    trend={marketTrend}
                    width={axisW}
                    height={MARKET_CURVE_H}
                    ariaLabel={`强势题材情绪走势, 最新 ${fmtScore(marketTrend.last?.score)}`}
                    hint={(d) => `${axis[d.i]} · 前20均值 ${fmtScore(d.score)}`}
                    label={
                      <>
                        <span className="text-[11px] text-foreground/60">情绪走势</span>
                        <span className="font-mono text-[12px] font-medium text-primary">{fmtScore(marketTrend.last?.score)}</span>
                      </>
                    }
                  />
                </div>
              ) : null}
              <div className="space-y-0.5">
                {visibleItems.map((it) => {
                  const byDate = cellsByDate(it.cells)
                  const faded = it.in_top_today === false && (it.top_days ?? 0) > 0
                  const fresh = it.first_top_date != null && it.first_top_date === latestDate
                  return (
                    <button
                      key={it.block_code}
                      type="button"
                      onClick={() => setActive(it.block_code)}
                      className={`group flex w-max min-w-full items-center gap-1 rounded ${
                        active === it.block_code ? 'bg-accent/50' : 'hover:bg-accent/30'
                      }`}
                    >
                      <span
                        title={
                          `${it.block_name || it.block_code}`
                          + (it.top_days ? `\n窗口内在榜 ${it.top_days} 天(${it.first_top_date} ~ ${it.last_top_date})` : '')
                        }
                        className={`sticky left-0 z-10 w-[76px] shrink-0 truncate bg-background px-1 text-left text-[11px] ${
                          active === it.block_code ? 'font-semibold text-primary' : faded ? 'text-muted-foreground' : ''
                        }`}
                      >
                        {fresh ? <span className="mr-0.5 text-primary">新</span> : null}
                        {faded ? <span className="mr-0.5 text-muted-foreground">退</span> : null}
                        {it.block_name || it.block_code}
                      </span>
                      <div className="flex gap-0.5">
                        {axis.map((d, i) => {
                          const c = byDate.get(d)
                          return (
                            <span
                              key={d}
                              title={`${it.block_name || it.block_code} · ${d} · 情绪分 ${fmtScore(c?.score)} · 涨停 ${c?.limit_up_cnt ?? 0}`}
                              className={`flex h-[22px] w-[38px] shrink-0 items-center justify-center rounded text-[10px] ${cellColorClass(
                                c?.score,
                              )} ${cellTextClass(c?.score)} ${
                                i === axis.length - 1 ? 'ring-1 ring-inset ring-primary/50' : ''
                              }`}
                            >
                              {c?.score == null ? '--' : Math.round(c.score)}
                            </span>
                          )
                        })}
                      </div>
                    </button>
                  )
                })}
              </div>
              {items.length > 15 ? (
                <button
                  type="button"
                  onClick={() => setShowAllRows((v) => !v)}
                  className="mt-1 w-full rounded border border-border/50 py-1 text-center text-[11px] text-muted-foreground hover:bg-accent/40"
                >
                  {showAllRows ? '收起' : `展开其余 ${items.length - 15} 行(含窗口内退榜题材)`}
                </button>
              ) : null}
            </div>
          </div>

          {detail ? (
            <div className="mt-3 rounded border border-border/60 p-3 text-[12px]">
              <div className="mb-2 flex items-center gap-2">
                <span className="font-semibold">{detail.block_name || detail.block_code}</span>
                <span className="text-[10px] text-muted-foreground">
                  {detail.block_type === 'industry' ? '行业' : '概念'} · {detail.block_code}
                </span>
                <span className="ml-auto text-[10px] text-muted-foreground">历史分位/连续性使用当期成分回看</span>
              </div>
              <div className="grid grid-cols-5 gap-2">
                {DIMS.map((label, i) => (
                  <div key={label} className="rounded bg-accent/40 p-2">
                    <div className="text-[10px] text-muted-foreground">{label}</div>
                    <div className={`font-mono text-[13px] ${cellTextClass(dims[i])}`}>{fmtScore(dims[i])}</div>
                  </div>
                ))}
              </div>
              {detailTrend ? (
                <div className="mt-3">
                  <div className="mb-1 flex items-baseline gap-2 text-[11px]">
                    <span className="text-foreground/70">情绪走势(近 {axis.length} 个交易日)</span>
                    <span className="ml-auto text-muted-foreground">
                      最高 {fmtScore(detailTrend.hi)} · 最低 {fmtScore(detailTrend.lo)} · 最新{' '}
                      <span className="font-mono text-foreground">{fmtScore(detailTrend.last?.score)}</span>
                    </span>
                  </div>
                  <div ref={detailScrollRef} className="scrollbar overflow-x-auto pb-1">
                    <TrendChart
                      trend={detailTrend}
                      width={axisW}
                      height={DETAIL_CURVE_H}
                      ariaLabel={`${detail.block_name || detail.block_code} 情绪走势, 最新 ${fmtScore(detailTrend.last?.score)}`}
                      hint={(d) => `${axis[d.i]} · 情绪分 ${fmtScore(d.score)}`}
                    />
                  </div>
                </div>
              ) : null}
              {detail.core_stocks.length > 0 ? (
                <div className="mt-2 flex flex-wrap gap-2">
                  {detail.core_stocks.slice(0, 2).map((s) => (
                    <span key={s.symbol} className="rounded bg-accent/40 px-2 py-1 text-[11px]">
                      {s.name || s.symbol} · {s.boards ?? '--'}板 · 核心分 {fmtScore(s.score)}
                      {s.prob != null ? ` · 连续概率 ${Math.round(s.prob * 100)}%` : ''}
                    </span>
                  ))}
                </div>
              ) : null}
            </div>
          ) : (
            <div className="mt-3 rounded border border-dashed border-border/60 p-4 text-center text-[11px] text-muted-foreground">
              点击左侧题材查看五维明细与核心股
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
