import { useEffect, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import { fmtAmount, fmtPct } from '@panwatch/biz-ui/lib/ladder-format'

/**
 * 个股 hover 预览卡(v0.5.93 工作台③, 老板"模态降级为 hover 预览")。
 * 悬停个股行时浮现: 现价/涨跌幅/封单/PE/次新/板块; 点击行跳 /stocks/:symbol 工作台。
 * 数据 on-demand 单股(/stocks/{sym}/l2 + /blocks), 缺值 '--' 不编; 不轮询。
 */
export interface HoverPreviewData {
  symbol: string
  name?: string | null
}

interface L2Lite {
  snapshot?: { now?: number | null; last_close?: number | null } | null
  more?: { fcamo?: number | null; pe_ttm?: number | null; ever_zt_count?: number | null } | null
}

export default function StockHoverPreview({
  data, x, y,
}: { data: HoverPreviewData | null; x: number; y: number }) {
  const [l2, setL2] = useState<L2Lite | null>(null)
  const [blocks, setBlocks] = useState<{ name: string; type: string }[]>([])
  const symbol = data?.symbol ?? ''
  useEffect(() => {
    if (!symbol) {
      setL2(null)
      setBlocks([])
      return
    }
    let alive = true
    fetchAPI<L2Lite>(`/stocks/${encodeURIComponent(symbol)}/l2`)
      .then((r) => { if (alive) setL2(r ?? null) })
      .catch(() => { if (alive) setL2(null) })
    fetchAPI<{ blocks?: { name: string; type: string }[] }>(`/stocks/${encodeURIComponent(symbol)}/blocks`)
      .then((r) => { if (alive) setBlocks(r?.blocks ?? []) })
      .catch(() => { if (alive) setBlocks([]) })
    return () => { alive = false }
  }, [symbol])
  if (!data) return null
  const s = l2?.snapshot ?? {}
  const m = l2?.more ?? {}
  const pct = s.now != null && s.last_close ? ((s.now - s.last_close) / s.last_close) * 100 : null
  return (
    <div
      className="pointer-events-none fixed z-50 w-[220px] rounded border border-border/70 bg-background/95 p-2 shadow-lg"
      style={{ left: Math.min(x + 12, window.innerWidth - 240), top: Math.min(y + 12, window.innerHeight - 160) }}
    >
      <div className="mb-1 flex items-baseline gap-1 text-[12px] font-semibold">
        {data.name || data.symbol}
        <span className="text-[10px] font-normal text-muted-foreground">{data.symbol}</span>
        {m.ever_zt_count ? (
          <span className="rounded bg-[--stock-up]/20 px-0.5 text-[9px] text-[--stock-up]">{m.ever_zt_count}板</span>
        ) : null}
      </div>
      <div className="flex justify-between text-[11px]">
        <span className="text-muted-foreground">现价</span>
        <span className="font-mono">{s.now != null ? s.now : '--'}</span>
      </div>
      <div className="flex justify-between text-[11px]">
        <span className="text-muted-foreground">涨跌</span>
        <span className={`font-mono ${pct == null ? 'text-muted-foreground' : pct >= 0 ? 'text-[--stock-up]' : 'text-[--stock-down]'}`}>
          {fmtPct(pct)}
        </span>
      </div>
      <div className="flex justify-between text-[11px]">
        <span className="text-muted-foreground">封单</span>
        <span className="font-mono">{m.fcamo != null ? fmtAmount(m.fcamo) : '--'}</span>
      </div>
      <div className="flex justify-between text-[11px]">
        <span className="text-muted-foreground">PE(TTM)</span>
        <span className="font-mono">{m.pe_ttm != null ? String(m.pe_ttm) : '--'}</span>
      </div>
      {blocks.length > 0 ? (
        <div className="mt-1 flex flex-wrap gap-0.5">
          {blocks.slice(0, 4).map((b) => (
            <span key={b.name} className="rounded bg-accent/50 px-0.5 text-[9px] text-foreground/80">{b.name}</span>
          ))}
        </div>
      ) : null}
      <div className="mt-1 text-[9px] text-muted-foreground">点击进个股工作台</div>
    </div>
  )
}
