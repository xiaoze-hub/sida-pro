import { useEffect, useState } from 'react'
import { fetchAPI } from '@panwatch/api'
import { useNavigate } from 'react-router-dom'
import { safeFixed } from '@/lib/format'

/**
 * 三指标共振清单卡片(2026-09-11, 决策先锋升级 B)。
 * 数据来自 GET /api/resonance/scan?only=resonance|near(盘后全市场扫描落库结果)。
 * 判定: 趋势(GS G区) × 强度(活跃度≥3) × 资金(净流入>0); 三项全对=共振, 两项=接近。
 * 行末 AI: 盘后 15:50 批量 LLM 复核结论(强共振/弱共振/未共振/无法判定), 悬停看摘要。
 */

interface ScanItem {
  trade_date: string
  symbol: string
  name: string | null
  trend: string | null
  activity: number | null
  level: string | null
  fund_net: number | null
  hits: number
  resonance: boolean
  near: boolean
  close: number | null
  change_pct: number | null
  ai_verdict: string | null
  ai_summary: string | null
  ai_confidence: number | null
}

function aiTag(it: ScanItem): { text: string; cls: string; tip: string } {
  if (!it.ai_verdict) {
    return { text: '--', cls: 'text-muted-foreground/50', tip: 'AI 判定待生成(盘后 15:50 自动批量)' }
  }
  const conf = it.ai_confidence == null ? '' : ` · 置信 ${Math.round(it.ai_confidence * 100)}%`
  const cls =
    it.ai_verdict === '强共振' ? 'text-stock-up' : it.ai_verdict === '弱共振' ? 'text-amber-500' : 'text-muted-foreground/70'
  return { text: it.ai_verdict, cls, tip: `AI ${it.ai_verdict}${conf}${it.ai_summary ? `\n${it.ai_summary}` : ''}` }
}

interface ScanResp {
  trade_date: string | null
  count: number
  items: ScanItem[]
}

function fmtYi(v: number | null): string {
  if (v == null || !Number.isFinite(v)) return '--'
  const yi = v / 1e8
  return `${yi > 0 ? '+' : ''}${safeFixed(yi, 2)}亿`
}

export default function ResonancePanel({ className }: { className?: string }) {
  const navigate = useNavigate()
  const [only, setOnly] = useState<'resonance' | 'near'>('resonance')
  const [resp, setResp] = useState<ScanResp | null>(null)

  useEffect(() => {
    let alive = true
    // 首页并发高(20+ 请求), 首拉易排队超时 → 失败/空结果保留上次数据并自动重试
    const load = async () => {
      try {
        const res = await fetchAPI<ScanResp>(`/resonance/scan?only=${only}&limit=30`, { cacheMode: 'reload' })
        if (alive && res?.items?.length) setResp(res)
        else if (alive) setResp((prev) => (prev && prev.items.length > 0 && prev.trade_date ? prev : { trade_date: null, count: 0, items: [] }))
      } catch {
        if (alive) setResp((prev) => prev ?? { trade_date: null, count: 0, items: [] })
      }
    }
    void load()
    const retry = window.setTimeout(() => void load(), 8000)
    const timer = window.setInterval(() => void load(), 60000)
    return () => {
      alive = false
      window.clearTimeout(retry)
      window.clearInterval(timer)
    }
  }, [only])

  return (
    <div className={className}>
      <div className="flex items-center gap-2">
        <span className="text-[13px] font-semibold">三指标共振</span>
        <span className="text-[10px] text-muted-foreground">趋势 × 活跃度 × 资金</span>
        {resp?.trade_date ? <span className="text-[10px] text-muted-foreground">· {resp.trade_date}</span> : null}
        <div className="ml-auto flex items-center gap-1">
          {(['resonance', 'near'] as const).map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => setOnly(k)}
              className={`rounded px-2 py-0.5 text-[11px] transition-colors ${
                only === k ? 'bg-primary text-primary-foreground' : 'bg-accent/50 text-muted-foreground hover:bg-accent'
              }`}
            >
              {k === 'resonance' ? '共振' : '接近共振'}
            </button>
          ))}
        </div>
      </div>
      {resp && resp.items.length === 0 ? (
        <div className="py-6 text-center text-[11px] text-muted-foreground">
          {only === 'resonance' ? '今日无三指标共振标的(等待盘后扫描)' : '暂无接近共振标的'}
        </div>
      ) : (
        <div className="mt-1.5 divide-y divide-border/40">
          {(resp?.items ?? []).map((it) => (
            <button
              key={it.symbol}
              type="button"
              onClick={() => navigate(`/quote/${it.symbol}`)}
              className="flex w-full items-center gap-2 py-1 text-left text-[12px] hover:bg-accent/40"
            >
              <span className="w-[70px] shrink-0 truncate font-medium">{it.name || it.symbol}</span>
              <span className="w-[58px] shrink-0 font-mono text-[11px] text-muted-foreground">{it.symbol}</span>
              <span className={`w-[46px] shrink-0 text-right font-mono ${(it.change_pct ?? 0) >= 0 ? 'text-stock-up' : 'text-stock-down'}`}>
                {it.change_pct == null ? '--' : `${it.change_pct > 0 ? '+' : ''}${safeFixed(it.change_pct, 2)}%`}
              </span>
              <span className="w-[64px] shrink-0 text-right font-mono text-[11px]">
                {it.activity == null ? '--' : safeFixed(it.activity, 2)}
              </span>
              <span className="w-[72px] shrink-0 text-right font-mono text-[11px] text-muted-foreground">{fmtYi(it.fund_net)}</span>
              <span className="ml-auto flex shrink-0 items-center gap-0.5 text-[10px]">
                <span className={it.hits >= 1 ? 'text-stock-up' : 'text-muted-foreground'}>趋</span>
                <span className={it.hits >= 2 ? 'text-stock-up' : 'text-muted-foreground'}>强</span>
                <span className={it.hits >= 3 ? 'text-stock-up' : 'text-muted-foreground'}>资</span>
              </span>
              <span className={`w-[52px] shrink-0 text-right text-[10px] ${aiTag(it).cls}`} title={aiTag(it).tip}>
                {aiTag(it).text}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
