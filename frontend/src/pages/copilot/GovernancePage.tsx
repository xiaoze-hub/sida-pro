/**
 * 治理页（2026-10-26，工作台 P2-10 补完）。
 *
 * 回答一个具体问题：「有没有哪一轮因为预算/并发被系统**真的拦下来**过？」
 * 数据来自审计表（`copilot_tool_budget_exceeded` / `copilot_concurrency_exceeded`），
 * 只回查已发生的事实 —— 空列表 = 没被拦过，**不**代表"不会拦"。
 */
import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, ShieldAlert } from 'lucide-react'
import { fetchAPI } from '@panwatch/api'
import { Skeleton } from '@/components/Skeleton'

export interface Interception {
  action: string
  detail: string
  created_at: string
}

const ACTION_LABEL: Record<string, string> = {
  copilot_tool_budget_exceeded: '工具调用预算超限',
  copilot_concurrency_exceeded: '并发上限',
}

export function actionLabel(action: string): string {
  return ACTION_LABEL[action] ?? action
}

/** `run=xxx used=12 limit=11` → 可读的事实；认不出的片段原样保留，不猜。 */
export function detailFacts(detail: string): { runId: string | null; parts: string[] } {
  const parts: string[] = []
  let runId: string | null = null
  for (const token of String(detail ?? '').split(/\s+/)) {
    if (!token) continue
    const [k, v] = token.split('=')
    if (k === 'run' && v) {
      runId = v
      continue
    }
    parts.push(token)
  }
  return { runId, parts }
}

export default function GovernancePage() {
  const [items, setItems] = useState<Interception[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    setError(null)
    try {
      const body = await fetchAPI<{ count: number; items: Interception[] }>(
        '/copilot/governance/interceptions',
        { signal },
      )
      setItems(body.items)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
      setItems(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    const ctrl = new AbortController()
    void load(ctrl.signal)
    return () => ctrl.abort()
  }, [load])

  return (
    <div className="page-container flex min-w-0 flex-col gap-4">
      <header className="flex items-end justify-between gap-3">
        <div>
          <h1 className="text-[20px] font-semibold text-foreground">治理 · 拦截记录</h1>
          <p className="mt-1 text-[12px] text-muted-foreground">
            预算超限与并发超限由系统**在运行中直接中止**，这里列出本人被拦过的记录。空 = 没被拦过，不代表不会拦。
          </p>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          className="inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-[12px] text-foreground hover:bg-muted"
        >
          <RefreshCw className="h-3 w-3" />
          刷新
        </button>
      </header>

      {loading ? (
        <div className="flex flex-col gap-2">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      ) : error ? (
        <div className="card-surface rounded-xl bg-card p-4 text-[14px] text-stock-down">
          读取失败：{error}
        </div>
      ) : !items || items.length === 0 ? (
        <div className="card-surface rounded-xl bg-card p-6 text-center text-[14px] text-muted-foreground">
          没有拦截记录
        </div>
      ) : (
        <div className="card-surface overflow-x-auto rounded-xl bg-card">
          <table className="w-full border-collapse text-[12px]">
            <thead>
              <tr className="border-b border-border text-left text-muted-foreground">
                <th className="px-3 py-2 font-medium">类型</th>
                <th className="px-3 py-2 font-medium">事实</th>
                <th className="px-3 py-2 font-medium">时间</th>
                <th className="px-3 py-2 font-medium">证据页</th>
              </tr>
            </thead>
            <tbody>
              {items.map((it, idx) => {
                const { runId, parts } = detailFacts(it.detail)
                return (
                  <tr key={`${it.action}-${idx}`} className="border-b border-border last:border-0">
                    <td className="px-3 py-2 text-foreground">
                      <span className="inline-flex items-center gap-1">
                        <ShieldAlert className="h-3 w-3" />
                        {actionLabel(it.action)}
                      </span>
                    </td>
                    <td className="px-3 py-2 tabular-nums text-foreground">{parts.join(' ') || '--'}</td>
                    <td className="px-3 py-2 tabular-nums text-muted-foreground">{it.created_at || '--'}</td>
                    <td className="px-3 py-2">
                      {runId ? (
                        <a
                          href={`/copilot/runs/${encodeURIComponent(runId)}`}
                          className="text-muted-foreground hover:text-foreground"
                        >
                          {runId.slice(0, 8)}…
                        </a>
                      ) : (
                        <span className="text-muted-foreground">--</span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
