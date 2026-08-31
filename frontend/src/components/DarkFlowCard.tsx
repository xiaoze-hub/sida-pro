import { useEffect, useState } from "react"

type DarkFlowData = {
  main_intent: string
  main_net: number
  dark_net: number
  outer_pct: number
  inner_pct: number
  mnemonic: { name: string; direction: string; text: string } | null
  divergence: boolean
}

export default function DarkFlowCard({ symbol }: { symbol: string }) {
  const [data, setData] = useState<DarkFlowData | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const token = localStorage.getItem("token") || ""
        const res = await fetch(`/api/dark-flow?symbol=${symbol}`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        })
        const j = await res.json()
        if (!cancelled && j?.code === 0) setData(j.data)
      } catch {}
      if (!cancelled) setLoading(false)
    }
    load()
    return () => { cancelled = true }
  }, [symbol])

  if (loading) return <div className="rounded-lg border p-3 animate-pulse h-24 bg-muted/30" />
  if (!data) return <div className="rounded-lg border p-3 text-sm text-muted-foreground">暂无暗盘数据</div>

  const outer = data.outer_pct ?? 0
  const inner = data.inner_pct ?? 0

  return (
    <div className="rounded-lg border bg-card p-3 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium">主力意图</span>
        <span className="text-sm font-semibold">{data.main_intent || "--"}</span>
      </div>
      <div className="flex items-center gap-2 text-xs">
        <span className="text-stock-up">外盘 {outer.toFixed(1)}%</span>
        <div className="flex-1 h-1.5 rounded bg-muted overflow-hidden flex">
          <div className="bg-stock-up h-full" style={{ width: `${outer}%` }} />
          <div className="bg-stock-down h-full" style={{ width: `${inner}%` }} />
        </div>
        <span className="text-stock-down">内盘 {inner.toFixed(1)}%</span>
      </div>
      {data.mnemonic && (
        <div className="text-xs rounded bg-muted/50 px-2 py-1">
          <span className="font-medium">{data.mnemonic.name}</span>
          <span className="text-muted-foreground"> · {data.mnemonic.direction}</span>
          <span className="ml-1 text-muted-foreground">{data.mnemonic.text}</span>
        </div>
      )}
      {data.divergence && (
        <div className="rounded bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 px-2 py-1.5 flex items-center justify-between">
          <span className="text-xs text-amber-700 dark:text-amber-400">⚠️ 内外盘与主力意图背离</span>
          <button
            className="text-xs px-2 py-1 rounded bg-amber-500 text-white hover:bg-amber-600"
            onClick={() => {
              const q = `主力意图显示${data.main_intent}，但内外盘外${outer.toFixed(0)}%内${inner.toFixed(0)}%与之背离，怎么理解？`
              window.dispatchEvent(new CustomEvent("sida:ask-ai", { detail: { question: q, symbol } }))
            }}
          >
            咨询AI助手
          </button>
        </div>
      )}
    </div>
  )
}
