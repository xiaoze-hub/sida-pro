import { useEffect, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import {
  AXIS_CELL_W,
  AXIS_PITCH,
  axisDates,
  cellColorClass,
  cellTextClass,
  cellsByDate,
  dayLabels,
  fmtScore,
  monthBands,
  type MoodCell,
} from '@/lib/theme-mood'

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
}

interface BoardResp {
  trade_date: string | null
  window: number
  count: number
  dates?: string[]
  items: MoodItem[]
}

const WINDOWS = [10, 20, 30] as const
const DIMS = ['涨停结构', '题材扩散', '核心强度', '接力反馈', '连续性'] as const

export default function ThemeMoodPage() {
  const [windowDays, setWindowDays] = useState<number>(20)
  const [resp, setResp] = useState<BoardResp | null>(null)
  const [active, setActive] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const res = await fetchAPI<BoardResp>(`/theme-mood/board?window=${windowDays}&top=15`, { cacheMode: 'reload' })
        if (alive) setResp(res)
      } catch {
        /* 保留旧数据 */
      }
    }
    void load()
    const timer = window.setInterval(() => void load(), 120000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [windowDays])

  const items = resp?.items ?? []
  const axis = axisDates(resp?.dates, items[0]?.cells ?? [], windowDays)
  const bands = monthBands(axis)
  const labels = dayLabels(axis)
  const latestDate = axis.length ? axis[axis.length - 1] : null
  const detail = items.find((it) => it.block_code === active) ?? null
  const dims = detail ? [detail.s1, detail.s2, detail.s3, detail.s4, detail.s5] : []

  return (
    <div className="mx-auto max-w-[1400px] p-4">
      <div className="mb-3 flex items-center gap-3">
        <h1 className="text-[15px] font-semibold">题材情绪</h1>
        <span className="rounded bg-accent px-1.5 py-0.5 text-[10px] text-muted-foreground">收盘确认口径</span>
        {resp?.trade_date ? <span className="text-[11px] text-muted-foreground">{resp.trade_date}</span> : null}
        <div className="ml-auto flex items-center gap-1">
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

      <div className="flex flex-col gap-3 xl:flex-row">
        <div className="w-full shrink-0 xl:w-[420px]">
          <div className="mb-1 grid grid-cols-[1fr_56px_56px_44px] gap-1 px-2 text-[10px] text-muted-foreground">
            <span>题材</span>
            <span className="text-right">情绪分</span>
            <span className="text-right">日变化</span>
            <span className="text-right">置信</span>
          </div>
          <div className="divide-y divide-border/40 rounded border border-border/60">
            {(resp?.items ?? []).map((it) => (
              <button
                key={it.block_code}
                type="button"
                onClick={() => setActive(it.block_code)}
                className={`grid w-full grid-cols-[1fr_56px_56px_44px] items-center gap-1 px-2 py-1.5 text-left text-[12px] hover:bg-accent/40 ${
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
          </div>
        </div>

        <div className="min-w-0 flex-1">
          <div className="rounded border border-border/60 p-2">
            <div className="mb-1 flex items-center gap-2 text-[11px] text-muted-foreground">
              <span>题材 × 日期(近 {windowDays} 个交易日 · 色块=情绪分)</span>
              {latestDate ? <span>最新 {latestDate}</span> : null}
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
            <div className="overflow-x-auto pb-1">
              <div className="flex w-max min-w-full items-end gap-1">
                <span className="sticky left-0 z-10 w-[76px] shrink-0 bg-background" />
                <div className="flex gap-0.5">
                  {bands.map((b) => (
                    <span
                      key={b.key}
                      className="border-b border-border/60 pb-0.5 text-center text-[9px] leading-3 text-muted-foreground"
                      style={{ width: b.count * AXIS_PITCH - (AXIS_PITCH - AXIS_CELL_W) }}
                    >
                      {b.label}
                    </span>
                  ))}
                </div>
              </div>
              <div className="mb-1 flex w-max min-w-full items-center gap-1">
                <span className="sticky left-0 z-10 w-[76px] shrink-0 bg-background text-[9px] leading-3 text-muted-foreground">
                  日期
                </span>
                <div className="flex gap-0.5">
                  {axis.map((d, i) => (
                    <span
                      key={d}
                      title={`${d}${i === axis.length - 1 ? ' · 最新收盘' : ''}`}
                      className={`w-[38px] shrink-0 text-center text-[9px] leading-3 ${
                        i === axis.length - 1 ? 'font-medium text-primary' : 'text-muted-foreground'
                      }`}
                    >
                      {labels[i]}
                    </span>
                  ))}
                </div>
              </div>
              <div className="space-y-0.5">
                {items.map((it) => {
                  const byDate = cellsByDate(it.cells)
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
                        title={it.block_name || it.block_code}
                        className={`sticky left-0 z-10 w-[76px] shrink-0 truncate bg-background px-1 text-left text-[11px] ${
                          active === it.block_code ? 'font-semibold text-primary' : ''
                        }`}
                      >
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
