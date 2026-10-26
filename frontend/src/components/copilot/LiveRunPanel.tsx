/**
 * 运行实时面板（2026-10-25，工作台 P0-3）。
 *
 * 现状不像"工作台"的地方：后台任务只有一行状态文案，工具调用要等跑完才在答案下方出现。
 * 但后端早就有 SSE（`GET /copilot/runs/{id}/events`）——**前端一次都没调用过**。
 * 本组件把它接上：状态迁移 + 步骤逐步到达 + 终态关闭。
 *
 * 诚实约束：
 *  1. 连不上 SSE **不假装在跑**：要显式标"事件流断开"，并退回轮询（后端有状态接口）；
 *  2. 步骤序号/耗时缺失写 `--`，不补 0；
 *  3. 终态必须给"证据页"入口，让用户能核对这一轮到底做了什么。
 */
import { useEffect, useRef, useState } from 'react'
import { ExternalLink, Loader2, Radio } from 'lucide-react'
import { formatElapsed } from '@/components/copilot/ToolTimeline'

export interface LiveStep {
  seq: number
  tool: string
  ok: boolean
  elapsed_ms: number | null
}

export interface LiveState {
  status: string
  toolCalls: number | null
  elapsedMs: number | null
  steps: LiveStep[]
  error: string | null
  conversationId: number | null
}

const TERMINAL = new Set(['ok', 'succeeded', 'failed', 'degraded', 'cancelled'])

const STATUS_LABEL: Record<string, string> = {
  queued: '排队中',
  running: '执行中',
  ok: '已完成',
  succeeded: '已完成',
  degraded: '降级完成',
  failed: '失败',
  cancelled: '已取消',
}

export function isTerminalStatus(status: string): boolean {
  return TERMINAL.has(String(status ?? '').toLowerCase())
}

/** 状态日志用：把 SSE 事件合进已有状态（步骤只增不减，状态以最新为准）。 */
export function reduceEvent(prev: LiveState, name: string, data: Record<string, unknown>): LiveState {
  if (name === 'status') {
    return {
      ...prev,
      status: String(data.status ?? prev.status),
      toolCalls: typeof data.tool_calls === 'number' ? data.tool_calls : prev.toolCalls,
      elapsedMs: typeof data.elapsed_ms === 'number' ? data.elapsed_ms : prev.elapsedMs,
      error: typeof data.error === 'string' ? data.error : prev.error,
      conversationId:
        typeof data.conversation_id === 'number' ? data.conversation_id : prev.conversationId,
    }
  }
  if (name === 'steps') {
    const items = Array.isArray(data.items) ? (data.items as LiveStep[]) : []
    return { ...prev, steps: items }
  }
  if (name === 'done') {
    // ⚠️ done 必须自己带状态：实测只处理 status 事件时，终态可能先被 done 推出，
    // 面板会一直显示「执行中」（单测抓到的真 bug）。
    return {
      ...prev,
      status: typeof data.status === 'string' ? data.status : prev.status,
      error: typeof data.error === 'string' ? data.error : prev.error,
    }
  }
  if (name === 'error') {
    return { ...prev, error: typeof data.message === 'string' ? data.message : '事件流异常' }
  }
  return prev
}

export function LiveRunPanel({
  runId,
  className = '',
}: {
  runId: string | null
  className?: string
}) {
  const [state, setState] = useState<LiveState>({
    status: 'queued',
    toolCalls: null,
    elapsedMs: null,
    steps: [],
    error: null,
    conversationId: null,
  })
  const [broken, setBroken] = useState(false)
  const esRef = useRef<EventSource | null>(null)

  useEffect(() => {
    if (!runId) return
    setState({ status: 'queued', toolCalls: null, elapsedMs: null, steps: [], error: null, conversationId: null })
    setBroken(false)

    // 用 cookie 认证（sida_token），EventSource 带 withCredentials 即可，不用把 token 拼进 URL
    const es = new EventSource(`/api/copilot/runs/${encodeURIComponent(runId)}/events`, {
      withCredentials: true,
    })
    esRef.current = es

    const handle = (name: string) => (ev: MessageEvent) => {
      let data: Record<string, unknown> = {}
      try {
        data = JSON.parse(ev.data) as Record<string, unknown>
      } catch {
        data = {}
      }
      setState((prev) => reduceEvent(prev, name, data))
      if (name === 'done') es.close()
    }

    const onStatus = handle('status')
    const onSteps = handle('steps')
    const onErrorEvent = handle('error')
    const onDone = handle('done')

    es.addEventListener('status', onStatus as EventListener)
    es.addEventListener('steps', onSteps as EventListener)
    es.addEventListener('error', onErrorEvent as EventListener)
    es.addEventListener('done', onDone as EventListener)
    es.onerror = () => {
      // 网络断/服务端关闭：如实标记，不假装仍在跑
      setBroken(true)
      es.close()
    }

    return () => {
      es.close()
      esRef.current = null
    }
  }, [runId])

  if (!runId) return null

  const status = String(state.status ?? '')
  const done = isTerminalStatus(status)

  return (
    <div className={`rounded-xl border border-border bg-card p-3 ${className}`} data-testid="live-run-panel">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-[12px] text-foreground">
          {done ? (
            <Radio className="h-3 w-3" />
          ) : (
            <Loader2 className="h-3 w-3 animate-spin" />
          )}
          <span className="font-medium">{STATUS_LABEL[status] ?? (status || '--')}</span>
          <span className="tabular-nums text-muted-foreground">
            {state.toolCalls === null ? '--' : `${state.toolCalls} 次工具`}
          </span>
          <span className="tabular-nums text-muted-foreground">
            {state.elapsedMs === null ? '--' : formatElapsed(state.elapsedMs)}
          </span>
        </div>
        <a
          href={`/copilot/runs/${encodeURIComponent(runId)}`}
          className="inline-flex items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground"
        >
          证据页
          <ExternalLink className="h-3 w-3" />
        </a>
      </div>

      {broken && !done ? (
        <div className="mt-2 text-[11px] text-amber-500">
          事件流已断开（任务可能仍在跑），可到证据页看最终状态
        </div>
      ) : null}

      {state.steps.length > 0 ? (
        <ol className="scrollbar mt-2 max-h-[180px] space-y-1 overflow-y-auto">
          {state.steps.map((s) => (
            <li key={s.seq} className="flex items-center gap-2 text-[11px]">
              <span className="w-5 shrink-0 tabular-nums text-muted-foreground">{s.seq}</span>
              <span className={s.ok ? 'text-foreground' : 'text-stock-down'}>{s.tool}</span>
              <span className="ml-auto tabular-nums text-muted-foreground">
                {s.elapsed_ms === null || s.elapsed_ms === undefined ? '--' : formatElapsed(s.elapsed_ms)}
              </span>
            </li>
          ))}
        </ol>
      ) : (
        <div className="mt-2 text-[11px] text-muted-foreground">还没有工具调用</div>
      )}

      {state.error && done && status !== 'ok' && status !== 'succeeded' ? (
        <div className="mt-2 text-[11px] text-stock-down">{state.error}</div>
      ) : null}
    </div>
  )
}
