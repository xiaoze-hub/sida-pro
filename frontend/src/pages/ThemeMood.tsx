import { useEffect, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import { cellColorClass, cellTextClass, fmtScore, splitWindows, type MoodCell } from '@/lib/theme-mood'

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

  const detail = resp?.items.find((it) => it.block_code === active) ?? null
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

      <div className="flex gap-3">
        <div className="w-[420px] shrink-0">
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
            <div className="mb-1 text-[11px] text-muted-foreground">
              题材 × 日期(近 {windowDays} 个交易日 · 色块=情绪分)
            </div>
            <div className="space-y-0.5">
              {(resp?.items ?? []).map((it) => (
                <div key={it.block_code} className="flex items-center gap-1">
                  <span className="w-[76px] shrink-0 truncate text-[11px]">{it.block_name || it.block_code}</span>
                  <div className="flex flex-wrap gap-0.5">
                    {splitWindows(it.cells, windowDays).map((c) => (
                      <span
                        key={c.date}
                        title={`${c.date} · 情绪分 ${fmtScore(c.score)} · 涨停 ${c.limit_up_cnt ?? 0}`}
                        className={`flex h-[22px] w-[38px] items-center justify-center rounded text-[10px] ${cellColorClass(
                          c.score,
                        )} ${cellTextClass(c.score)}`}
                      >
                        {c.score == null ? '--' : Math.round(c.score)}
                      </span>
                    ))}
                  </div>
                </div>
              ))}
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
