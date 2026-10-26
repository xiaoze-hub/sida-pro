import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { usePausedInterval } from '@panwatch/base-ui'
import { fetchAPI } from '@panwatch/api'
import { useNavigate } from 'react-router-dom'
import { safeFixed } from '@/lib/format'

/**
 * 三指标共振清单卡片(2026-09-11, 数智决策升级 B)。
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
    return { text: '--', cls: 'text-muted-foreground', tip: 'AI 判定待生成(盘后 15:50 自动批量)' }
  }
  const conf = it.ai_confidence == null ? '' : ` · 置信 ${Math.round(it.ai_confidence * 100)}%`
  const cls =
    it.ai_verdict === '强共振' ? 'text-stock-up' : it.ai_verdict === '弱共振' ? 'text-amber-500' : 'text-muted-foreground'
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

export default function ResonancePanel({ className, actions }: { className?: string; actions?: ReactNode }) {
  const navigate = useNavigate()
  const [only, setOnly] = useState<'resonance' | 'near'>('resonance')
  const [resp, setResp] = useState<ScanResp | null>(null)
  /** 走查 2026-09-18: 默认只展示 Top20 —— 40+ 行会把首页异动池/发现区推得很远 */
  const [expanded, setExpanded] = useState(false)
  const PREVIEW_N = 20

  const aliveRef = useRef(true)
  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
    }
  }, [])
  const load = useCallback(async () => {
    try {
      const res = await fetchAPI<ScanResp>(`/resonance/scan?only=${only}&limit=30`, { cacheMode: 'reload' })
      if (!aliveRef.current) return
      if (res?.items?.length) setResp(res)
      else setResp((prev) => (prev && prev.items.length > 0 && prev.trade_date ? prev : { trade_date: null, count: 0, items: [] }))
    } catch {
      if (aliveRef.current) setResp((prev) => prev ?? { trade_date: null, count: 0, items: [] })
    }
  }, [only])
  // 页签 keep-alive(2026-10-15): 隐藏页签停表, 回前台补一次。
  usePausedInterval(load, 60000, { deps: [only] })
  // 首页并发高(20+ 请求), 首拉易排队超时 → 8s 后一次性兜底重试(非周期轮询, 不进闸门)。
  useEffect(() => {
    const retry = window.setTimeout(() => void load(), 8000)
    return () => window.clearTimeout(retry)
  }, [load])

  const items = resp?.items ?? []
  const visible = expanded ? items : items.slice(0, PREVIEW_N)

  return (
    <div className={className}>
      <div className="flex items-center gap-2">
        <span className="text-[14px] font-semibold">三指标共振</span>
        <span className="text-[12px] text-muted-foreground">趋势 × 活跃度 × 资金</span>
        {resp?.trade_date ? <span className="text-[12px] text-muted-foreground">· {resp.trade_date}</span> : null}
        <div className="ml-auto flex items-center gap-1">
          {actions}
          {(['resonance', 'near'] as const).map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => setOnly(k)}
              className={`rounded px-2 py-0.5 text-[12px] transition-colors ${
                only === k ? 'bg-primary text-primary-foreground' : 'bg-s2 text-muted-foreground hover:bg-accent'
              }`}
            >
              {k === 'resonance' ? '共振' : '接近共振'}
            </button>
          ))}
        </div>
      </div>
      {resp && resp.items.length === 0 ? (
        <div className="py-6 text-center text-[12px] text-muted-foreground">
          {only === 'resonance' ? '今日无三指标共振标的(等待盘后扫描)' : '暂无接近共振标的'}
        </div>
      ) : (
        <div className="mt-1.5 divide-y divide-border/40">
          {/* 表头(2026-10-24): 此前只有裸数据行, 用户看到 32.43 / +0.55亿 只能猜列义。
              列宽必须与行完全一致, 否则数值列对不齐 —— 改行宽时两处一起改。 */}
          <div className="flex w-full items-center gap-2 pb-1 text-[12px] text-muted-foreground">
            <span className="w-[70px] shrink-0">名称</span>
            <span className="w-[58px] shrink-0">代码</span>
            <span className="w-[46px] shrink-0 text-right">涨幅</span>
            <span className="w-[64px] shrink-0 text-right" title="活跃度(≥3 视为强度达标)">活跃度</span>
            <span className="w-[72px] shrink-0 text-right">资金净额</span>
            <span className="ml-auto shrink-0">三指标</span>
            <span className="w-[52px] shrink-0 text-right" title="盘后 LLM 复核结论">AI</span>
          </div>
          {visible.map((it) => (
            <button
              key={it.symbol}
              type="button"
              onClick={() => navigate(`/quote/${it.symbol}`)}
              className="flex w-full items-center gap-2 py-1 text-left text-[12px] hover:bg-s2"
            >
              <span className="w-[70px] shrink-0 truncate font-medium" title={it.name || it.symbol}>{it.name || it.symbol}</span>
              <span className="w-[58px] shrink-0 font-mono text-[12px] text-muted-foreground">{it.symbol}</span>
              <span className={`w-[46px] shrink-0 text-right font-mono ${(it.change_pct ?? 0) >= 0 ? 'text-stock-up' : 'text-stock-down'}`}>
                {it.change_pct == null ? '--' : `${it.change_pct > 0 ? '+' : ''}${safeFixed(it.change_pct, 2)}%`}
              </span>
              <span className="w-[64px] shrink-0 text-right font-mono text-[12px]">
                {it.activity == null ? '--' : safeFixed(it.activity, 2)}
              </span>
              <span className="w-[72px] shrink-0 text-right font-mono text-[12px] text-muted-foreground">{fmtYi(it.fund_net)}</span>
              <span className="ml-auto flex shrink-0 items-center gap-0.5 text-[12px]">
                <span className={it.hits >= 1 ? 'text-stock-up' : 'text-muted-foreground'}>趋</span>
                <span className={it.hits >= 2 ? 'text-stock-up' : 'text-muted-foreground'}>强</span>
                <span className={it.hits >= 3 ? 'text-stock-up' : 'text-muted-foreground'}>资</span>
              </span>
              <span className={`w-[52px] shrink-0 text-right text-[12px] ${aiTag(it).cls}`} title={aiTag(it).tip}>
                {aiTag(it).text}
              </span>
            </button>
          ))}
          {items.length > PREVIEW_N && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="w-full py-1.5 text-center text-[12px] text-muted-foreground hover:text-foreground"
            >
              {expanded ? '收起' : `展开全部 ${items.length} 条`}
            </button>
          )}
        </div>
      )}
    </div>
  )
}
