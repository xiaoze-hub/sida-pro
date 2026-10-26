/**
 * 治理条（2026-10-26，工作台 P2-10）。
 *
 * 之前预算/白名单/Frozen Ring 全是**事后**才知道（跑完看轨迹，或 CI 报红）。
 * 这里把"这一轮到底受什么约束"摆在运行面板下方：预算上限与已用、工具白名单条数、
 * Frozen Ring 四条。
 *
 * 诚实约束：
 *  - 只读展示，**不做拦截**（真正的拦在 chat.py 的预算与 frozen 守卫里）；
 *  - 白名单 0 条 = "本轮未限定"，不是"允许所有"；
 *  - 缺上限就显示 `--`，不算余量（缺一边不能相减）。
 */
import { useEffect, useState } from 'react'
import { ShieldCheck } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'

export interface RunGovernance {
  run_id: string
  status: string
  recipe: string | null
  budget: { max: number | null; used: number | null; remaining: number | null }
  whitelist: { count: number; tools: string[] }
  frozen_ring: { guards: string[]; note: string }
  limits: { max_tool_calls: number | null; recipe_tools_declared: number }
  /** 本 run 真被拦过才有记录（预算/并发）；空 = 没被拦过，不是"不会拦" */
  interceptions?: { action: string; detail: string; created_at: string }[]
}

export const getRunGovernance = (runId: string, signal?: AbortSignal) =>
  fetchAPI<RunGovernance>(`/copilot/runs/${encodeURIComponent(runId)}/governance`, { signal })

export function budgetText(g: RunGovernance | null): string {
  if (!g) return '--'
  const { max, used } = g.budget
  const usedText = used === null || used === undefined ? '--' : String(used)
  const maxText = max === null || max === undefined ? '--' : String(max)
  return `${usedText} / ${maxText}`
}

export function whitelistText(g: RunGovernance | null): string {
  if (!g) return '--'
  return g.whitelist.count > 0 ? `${g.whitelist.count} 个工具` : '未限定'
}

export function GovernanceStrip({ runId }: { runId: string | null }) {
  const [data, setData] = useState<RunGovernance | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!runId) {
      setData(null)
      setError(null)
      return
    }
    const ctrl = new AbortController()
    getRunGovernance(runId, ctrl.signal)
      .then((d) => {
        setData(d)
        setError(null)
      })
      .catch((exc) => {
        setError(exc instanceof Error ? exc.message : String(exc))
        setData(null)
      })
    return () => ctrl.abort()
  }, [runId])

  if (!runId) return null

  return (
    <div
      className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border pt-2 text-[11px] text-muted-foreground"
      data-testid="governance-strip"
    >
      <span className="inline-flex items-center gap-1 text-foreground">
        <ShieldCheck className="h-3 w-3" />
        治理
      </span>
      {error ? (
        <span className="text-stock-down">读取失败：{error}</span>
      ) : (
        <>
          <span>
            预算 <span className="tabular-nums text-foreground">{budgetText(data)}</span>
          </span>
          <span>
            白名单 <span className="text-foreground">{whitelistText(data)}</span>
          </span>
          <span title={data?.frozen_ring.note ?? ''}>
            Frozen Ring <span className="tabular-nums text-foreground">{data ? data.frozen_ring.guards.length : '--'}</span> 条
          </span>
          {data && (data.interceptions?.length ?? 0) > 0 ? (
            <span
              className="text-stock-down"
              title={(data.interceptions ?? []).map((i) => i.detail).join('\n')}
            >
              被拦截 {data.interceptions?.length} 次
            </span>
          ) : null}
          <span>拦截记录可查</span>
        </>
      )}
    </div>
  )
}
