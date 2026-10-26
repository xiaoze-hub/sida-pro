import { fetchAPI } from '@panwatch/api'

import { safeFixed } from '@/lib/format'

/**
 * 参谋长任务（后台执行）客户端（2026-10-25，P0-0.2 前端）。
 *
 * 为什么要有这条路：同步流式一次深问要等 20~130s，关页面/断网/代理超时整轮就白烧。
 * 后台模式提交即返回 run_id，服务端继续跑，用户可离开；回来查状态即可。
 *
 * 诚实纪律：
 *  - **不假装进度百分比**——只有 queued/running/ok/failed/cancelled 与步数，没有分母就不算百分比；
 *  - 失败要把服务端给的 `error` 原文透出来，不吞成「出错了」。
 */

export interface SubmittedRun {
  run_id: string
  status: string
  events?: string
}

export interface RunStatus {
  run_id?: string
  status: string
  elapsed_ms?: number | null
  tool_calls?: number | null
  conversation_id?: number | null
  error?: string | null
}

/** 提交后台执行。conversationId 为空时服务端会新建会话。 */
export async function submitCopilotRun(
  question: string,
  conversationId?: number | null,
  signal?: AbortSignal,
): Promise<SubmittedRun> {
  return fetchAPI<SubmittedRun>('/copilot/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    // fetchAPI 不自动 stringify（只补 Content-Type），body 必须是字符串
    body: JSON.stringify({ question, conversation_id: conversationId ?? null }),
    signal,
  })
}

/**
 * 查一次状态。
 *
 * 后端 `GET /copilot/runs/{id}` 返回 `{run:{...}, steps:[...]}`，这里拍平成 status，
 * 让调用方不必知道详情接口的形状（列表/详情/状态三处形状不同，容易踩）。
 */
export async function getCopilotRunStatus(runId: string, signal?: AbortSignal): Promise<RunStatus> {
  const raw = await fetchAPI<{ run?: RunStatus } & RunStatus>(`/copilot/runs/${encodeURIComponent(runId)}`, {
    signal,
  })
  const run = (raw && 'run' in raw && raw.run) || (raw as RunStatus)
  return {
    run_id: run.run_id ?? runId,
    status: run.status ?? 'unknown',
    elapsed_ms: run.elapsed_ms ?? null,
    tool_calls: run.tool_calls ?? null,
    conversation_id: run.conversation_id ?? null,
    error: run.error ?? null,
  }
}

const TERMINAL = new Set(['ok', 'degraded', 'error', 'failed', 'succeeded', 'cancelled'])

export function isTerminalStatus(status: string | null | undefined): boolean {
  return TERMINAL.has(status ?? '')
}

/**
 * 进度文案（纯函数，便于测试）。
 * 缺值一律不给数字：`tool_calls` 为 null 时写「已提交」，不写「0 步」——0 步是"跑了但没调工具"，
 * 和"还没开始"是两件事。
 */
export function progressText(s: RunStatus | null): string {
  if (!s) return '已提交，等待执行…'
  const steps = typeof s.tool_calls === 'number' ? `${s.tool_calls} 步` : null
  const secs = typeof s.elapsed_ms === 'number' ? `${safeFixed(s.elapsed_ms / 1000, 1)}s` : null

  switch (s.status) {
    case 'queued':
      return '已排队，等待执行…'
    case 'running':
      return ['后台执行中…', steps].filter(Boolean).join(' · ')
    case 'ok':
    case 'succeeded':
      return ['已完成', steps, secs].filter(Boolean).join(' · ')
    case 'degraded':
      return ['完成（部分工具失败）', steps, secs].filter(Boolean).join(' · ')
    case 'failed':
    case 'error':
      return `执行失败${s.error ? `：${s.error}` : ''}`
    case 'cancelled':
      return '已取消'
    default:
      return `状态：${s.status}`
  }
}
