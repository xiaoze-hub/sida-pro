/**
 * 并行诊断页（2026-10-25，工作台 P1-7 前端）。
 *
 * 一次提交 2~4 个子问题 → 每个子 run 一条独立轨迹（预算各自独立、失败不重跑）→
 * 按子任务分列展示事实与缺口。
 *
 * ⚠️ 一个必须讲清的事实：这里**不能勾选配方**。配方路由由 chat.py 内的
 * `route_for_request` 自动决定（只接受工具白名单，不接受配方 id），要强制指定就得改
 * chat.py（2310 行护栏挡住）。所以本页展示的是每个子 run **实际命中**的配方，
 * 而不是"我让它用哪套配方"——后者是假的。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Loader2, Play } from 'lucide-react'
import { formatElapsed } from '@/components/copilot/ToolTimeline'
import { getParallel, submitParallel } from '@/lib/copilot/parallel'
import type { ParallelState } from '@/lib/copilot/parallel'

const MAX_ITEMS = 4
const POLL_MS = 3000

const STATUS_LABEL: Record<string, string> = {
  queued: '排队中',
  running: '执行中',
  ok: '完成',
  failed: '失败',
  degraded: '降级',
  cancelled: '取消',
}

const TERMINAL = new Set(['ok', 'failed', 'degraded', 'cancelled'])

export function splitQuestions(raw: string): string[] {
  return raw
    .split('\n')
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
}

export default function ParallelDiagnosis() {
  const [raw, setRaw] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [runId, setRunId] = useState<string | null>(null)
  const [state, setState] = useState<ParallelState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const timer = useRef<number | null>(null)

  const items = splitQuestions(raw)
  const tooMany = items.length > MAX_ITEMS

  const poll = useCallback(async (id: string) => {
    try {
      const body = await getParallel(id)
      setState(body)
      if (TERMINAL.has(String(body.status).toLowerCase())) {
        if (timer.current) window.clearTimeout(timer.current)
        timer.current = null
        return
      }
      timer.current = window.setTimeout(() => void poll(id), POLL_MS)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [])

  useEffect(() => {
    return () => {
      if (timer.current) window.clearTimeout(timer.current)
    }
  }, [])

  const submit = async () => {
    if (submitting || items.length === 0 || tooMany) return
    setSubmitting(true)
    setError(null)
    setState(null)
    try {
      const res = await submitParallel(items)
      setRunId(res.run_id)
      timer.current = window.setTimeout(() => void poll(res.run_id), POLL_MS)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setSubmitting(false)
    }
  }

  const summary = state?.summary
  const done = state ? TERMINAL.has(String(state.status).toLowerCase()) : false

  return (
    <div className="page-container flex min-w-0 flex-col gap-4">
      <header>
        <h1 className="text-[20px] font-semibold text-foreground">并行诊断</h1>
        <p className="mt-1 text-[12px] text-muted-foreground">
          一次提 2~4 个子问题，每个子任务独立轨迹、独立预算。配方由路由自动决定，本页显示实际命中的配方。
        </p>
      </header>

      <div className="card-surface rounded-xl bg-card p-3">
        <label className="block text-[12px] text-muted-foreground" htmlFor="parallel-input">
          子问题（一行一个，最多 {MAX_ITEMS} 个）
        </label>
        <textarea
          id="parallel-input"
          value={raw}
          onChange={(e) => setRaw(e.target.value)}
          rows={4}
          placeholder={'600111 现在能追吗\n今天大盘情绪如何'}
          className="mt-1 w-full resize-none rounded-lg border border-border bg-s0 px-2 py-1.5 text-[12px] text-foreground outline-none"
        />
        <div className="mt-2 flex items-center gap-3">
          <button
            type="button"
            onClick={() => void submit()}
            disabled={submitting || items.length === 0 || tooMany}
            className="inline-flex items-center gap-1 rounded-lg border border-border px-3 py-1 text-[12px] text-foreground hover:bg-muted disabled:cursor-not-allowed disabled:text-muted-foreground"
          >
            {submitting ? <Loader2 className="h-3 w-3 animate-spin" /> : <Play className="h-3 w-3" />}
            开始诊断
          </button>
          <span className="tabular-nums text-[12px] text-muted-foreground">
            {items.length} / {MAX_ITEMS}
            {tooMany ? ' · 超出上限，请拆成多批' : ''}
          </span>
        </div>
      </div>

      {error ? (
        <div className="card-surface rounded-xl bg-card p-3 text-[14px] text-stock-down">
          提交失败：{error}
        </div>
      ) : null}

      {runId && !state ? (
        <div className="card-surface rounded-xl bg-card p-3 text-[12px] text-muted-foreground">
          已提交（{runId.slice(0, 8)}…），等待子任务登记…
        </div>
      ) : null}

      {summary ? (
        <>
          <div className="card-surface flex flex-wrap items-center gap-3 rounded-xl bg-card p-3 text-[12px]">
            <span className="font-medium text-foreground">
              {STATUS_LABEL[String(state?.status ?? '')] ?? state?.status}
            </span>
            <span className="tabular-nums text-muted-foreground">子任务 {summary.count}</span>
            <span className="tabular-nums text-muted-foreground">完成 {summary.ok}</span>
            <span className="tabular-nums text-muted-foreground">失败 {summary.failed}</span>
            <span className="tabular-nums text-muted-foreground">工具 {summary.tool_calls}</span>
            {done ? null : <span className="text-muted-foreground">每 {POLL_MS / 1000}s 刷新</span>}
          </div>

          {summary.children.length === 0 ? (
            <div className="card-surface rounded-xl bg-card p-4 text-center text-[12px] text-muted-foreground">
              {summary.reason || '还没有子任务'}
            </div>
          ) : (
            <div className="card-surface overflow-x-auto rounded-xl bg-card">
              <table className="w-full border-collapse text-[12px]">
                <thead>
                  <tr className="border-b border-border text-left text-muted-foreground">
                    <th className="px-3 py-2 font-medium">子问题</th>
                    <th className="px-3 py-2 font-medium">命中配方</th>
                    <th className="px-3 py-2 font-medium">状态</th>
                    <th className="px-3 py-2 font-medium text-right">工具</th>
                    <th className="px-3 py-2 font-medium text-right">耗时</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.children.map((c) => (
                    <tr key={c.run_id} className="border-b border-border last:border-0">
                      <td className="max-w-[360px] truncate px-3 py-2 text-foreground" title={c.question}>
                        {c.question || '--'}
                      </td>
                      <td className="px-3 py-2 text-foreground">{c.recipe_id || '未命中'}</td>
                      <td className={c.status === 'failed' ? 'px-3 py-2 text-stock-down' : 'px-3 py-2 text-foreground'}>
                        {STATUS_LABEL[c.status] ?? c.status}
                        {c.error ? <span className="ml-1 text-[12px]">({c.error})</span> : null}
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums text-foreground">
                        {c.tool_calls === null || c.tool_calls === undefined ? '--' : c.tool_calls}
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums text-foreground">
                        {c.elapsed_ms === null || c.elapsed_ms === undefined ? '--' : formatElapsed(c.elapsed_ms)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {summary.gaps.length > 0 ? (
            <div className="card-surface rounded-xl bg-card p-3">
              <div className="text-[12px] font-medium text-foreground">缺口（未完成的子任务）</div>
              <ul className="mt-1 space-y-1">
                {summary.gaps.map((g) => (
                  <li key={g} className="text-[12px] text-amber-500">
                    {g}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  )
}
