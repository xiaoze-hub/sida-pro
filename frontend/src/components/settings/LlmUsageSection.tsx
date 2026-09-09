/** W5.3: AI 调用统计区块(从 Settings.tsx 抽出, 逐字保留)。 */

import { useEffect, useState } from 'react'
import { Activity } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'

interface LlmUsageItem {
  time: string
  scene: string
  scene_key: string
  model: string
  tokens: number
  latency: string
}
interface LlmUsageData {
  summary: { calls: number; tokens: number; cost: number; month_calls: number; month_cost: number; month_tokens: number }
  items: LlmUsageItem[]
  scenes: { key: string; label: string }[]
  note?: string
}

export function LlmUsageSection() {
  const [range, setRange] = useState<'day' | '7d' | '30d'>('day')
  const [scene, setScene] = useState('')
  const [data, setData] = useState<LlmUsageData | null>(null)
  const [loading, setLoading] = useState(true)

  const load = async (r: 'day' | '7d' | '30d', s: string) => {
    setLoading(true)
    try {
      const d = await fetchAPI<LlmUsageData>(`/llm-usage?range=${r}${s ? `&scene=${s}` : ''}`, { cacheMode: 'reload' })
      setData(d)
    } catch {
      setData(null)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load(range, scene) }, [range, scene])

  const fmtNum = (n: number) => (n ?? 0).toLocaleString('en-US')
  const fmtCost = (n: number) => `¥${(n ?? 0).toFixed(2)}`

  return (
    <section id="sec-llm-usage" className="border-t border-border/40 pt-4 md:pt-5 lg:col-span-12">
      <div className="mb-3 flex items-center gap-2">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          <Activity className="h-4 w-4 text-primary" />
          AI 调用统计
        </h2>
        <span className="text-[11px] text-muted-foreground">LLM token 用量与费用估算</span>
      </div>

      {/* 区间 + 场景筛选(分组标签) */}
      <div className="flex flex-wrap items-center gap-1.5 mb-3">
        <span className="text-[10px] text-muted-foreground mr-0.5">时间范围</span>
        {(['day', '7d', '30d'] as const).map(r => (
          <button
            key={r}
            onClick={() => setRange(r)}
            aria-pressed={range === r}
            className={`rounded-md px-2.5 py-1.5 text-[11px] transition-colors ${
              range === r ? 'bg-primary/15 text-primary' : 'text-muted-foreground hover:bg-accent'
            }`}
          >
            {r === 'day' ? '今日' : r === '7d' ? '近7天' : '近30天'}
          </button>
        ))}
        <span className="text-[10px] text-muted-foreground ml-3 mr-0.5">场景</span>
        <button
          onClick={() => setScene('')}
          aria-pressed={scene === ''}
          className={`rounded-md px-2.5 py-1.5 text-[11px] transition-colors ${
            scene === '' ? 'bg-primary/15 text-primary' : 'text-muted-foreground hover:bg-accent'
          }`}
        >
          全部
        </button>
        {(data?.scenes || []).map(s => (
          <button
            key={s.key}
            onClick={() => setScene(s.key)}
            aria-pressed={scene === s.key}
            className={`rounded-md px-2.5 py-1.5 text-[11px] transition-colors ${
              scene === s.key ? 'bg-primary/15 text-primary' : 'text-muted-foreground hover:bg-accent'
            }`}
          >
            {s.label}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="py-4 text-[12px] text-muted-foreground">加载中…</div>
      ) : data ? (
        <>
          {/* 汇总格: 区间 3 + 本月 2 明确分组 */}
          <div className="grid grid-cols-3 gap-2 mb-1.5">
            {[
              ['区间调用', fmtNum(data.summary.calls), '次'],
              ['区间 Token', fmtNum(data.summary.tokens), ''],
              ['区间费用', fmtCost(data.summary.cost), '估算'],
            ].map(([label, val, suffix]) => (
              <div key={label} className="rounded-md border border-border/40 bg-accent/20 px-3 py-2.5">
                <div className="text-[10px] text-muted-foreground">{label}</div>
                <div className="font-mono text-[14px] font-semibold text-foreground tabular-nums">
                  {val}
                  {suffix && <span className="ml-1 text-[10px] font-normal text-muted-foreground">{suffix}</span>}
                </div>
              </div>
            ))}
          </div>
          <div className="grid grid-cols-2 gap-2 mb-3">
            {[
              ['本月调用', fmtNum(data.summary.month_calls), '次'],
              ['本月费用', fmtCost(data.summary.month_cost), '估算'],
            ].map(([label, val, suffix]) => (
              <div key={label} className="rounded-md border border-border/40 bg-accent/20 px-3 py-2.5">
                <div className="text-[10px] text-muted-foreground">{label}</div>
                <div className="font-mono text-[14px] font-semibold text-foreground tabular-nums">
                  {val}
                  {suffix && <span className="ml-1 text-[10px] font-normal text-muted-foreground">{suffix}</span>}
                </div>
              </div>
            ))}
          </div>

          {/* 明细表(折叠: 默认收起, 点开才展示日志明细) */}
          <details className="mt-3 group">
            <summary className="flex cursor-pointer items-center gap-1.5 text-[12px] text-muted-foreground select-none hover:text-foreground transition-colors">
              <span className="inline-block transition-transform group-open:rotate-90">▶</span>
              调用明细
              {data.items.length > 0 && (
                <span className="text-[10px] text-muted-foreground/70">(最近 {data.items.length} 条)</span>
              )}
            </summary>
            <div className="mt-2">
              {data.items.length === 0 ? (
                <div className="py-3 text-center text-[12px] text-muted-foreground">暂无调用记录</div>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-[12px]">
                    <thead>
                      <tr className="text-left text-[10px] text-muted-foreground border-b border-border/40">
                        <th className="py-1.5 pr-3 font-medium">时间</th>
                        <th className="py-1.5 pr-3 font-medium">场景</th>
                        <th className="py-1.5 pr-3 font-medium">模型</th>
                        <th className="py-1.5 pr-3 font-medium text-right">Tokens</th>
                        <th className="py-1.5 font-medium text-right">耗时</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.items.map((it, i) => (
                        <tr key={i} className="border-b border-border/20 hover:bg-accent/30 transition-colors">
                          <td className="py-1.5 pr-3 text-muted-foreground tabular-nums whitespace-nowrap">{it.time}</td>
                          <td className="py-1.5 pr-3">
                            <span className="rounded bg-accent/40 px-1.5 py-0.5 text-[10px] text-muted-foreground">{it.scene}</span>
                          </td>
                          <td className="py-1.5 pr-3 font-mono text-[11px] text-muted-foreground">{it.model}</td>
                          <td className="py-1.5 pr-3 text-right font-mono tabular-nums">{fmtNum(it.tokens)}</td>
                          <td className="py-1.5 text-right text-muted-foreground tabular-nums">{it.latency}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </details>

          {data.note && (
            <p className="mt-2 text-[10px] text-muted-foreground">{data.note}</p>
          )}
        </>
      ) : (
        <div className="py-4 flex items-center justify-center gap-2">
          <span className="text-[12px] text-muted-foreground">加载失败</span>
          <button
            onClick={() => void load(range, scene)}
            className="rounded-md px-2 py-1 text-[11px] text-primary hover:bg-accent"
          >
            重试
          </button>
        </div>
      )}
    </section>
  )
}
