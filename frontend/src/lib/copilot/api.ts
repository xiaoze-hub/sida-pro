/** 参谋接口（走 fetchAPI，自带 /api 前缀，这里**不要**再写 /api/copilot）。 */
import { fetchAPI } from '@panwatch/api'
import type { CopilotBrief, CopilotPreset } from './types'

export const getCopilotBrief = (
  symbol: string,
  market = 'CN',
  refresh = false,
  signal?: AbortSignal,
) =>
  fetchAPI<CopilotBrief>(
    `/copilot/brief/${encodeURIComponent(symbol)}?market=${encodeURIComponent(market)}${
      refresh ? '&refresh=true' : ''
    }`,
    { signal, cacheMode: refresh ? 'reload' : undefined },
  )

export const getCopilotPresets = (signal?: AbortSignal) =>
  fetchAPI<CopilotPreset[]>('/copilot/presets', { signal })

export interface MentionStock {
  type: 'stock'
  symbol: string
  name: string
  market?: string
  py?: string
}

export interface MentionAgent {
  type: 'agent'
  id: string
  label: string
  icon: string
  desc: string
  when: string[]
  tool_count: number
}

/** `@` 面板候选：自选股 + 内置智能体（服务端已按用户隔离）。 */
export function getCopilotMentions(q = ''): Promise<{ stocks: MentionStock[]; agents: MentionAgent[] }> {
  return fetchAPI(`/copilot/mentions?q=${encodeURIComponent(q)}`, { timeoutMs: 8000 })
}

export function getCopilotAgents(): Promise<MentionAgent[]> {
  return fetchAPI('/copilot/agents', { timeoutMs: 8000 })
}

/** 页面即上下文（P1）：把当前视图推给后端，提问时自动带上标的/周期/圈选区。 */
export interface ViewStatePayload {
  conversation_id?: number | null
  page?: string
  symbol?: string
  market?: string
  period?: string
  range?: { from?: string; to?: string }
  selection?: { from?: string; to?: string } | null
  indicators?: string[]
  anchor_line?: number | null
}

export const reportViewState = (payload: ViewStatePayload) =>
  fetchAPI<{ saved: boolean }>('/copilot/context', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })

/** 动作回写（P1）：AI 只能提议，用户点确认才落库。 */
export interface PendingAction {
  id: number
  kind: 'annotation' | 'watchlist' | 'alert'
  payload: Record<string, unknown>
  status: string
  result?: string | null
  created_at: string
}

export const getPendingActions = (signal?: AbortSignal) =>
  fetchAPI<PendingAction[]>('/copilot/actions', { signal })

export const applyPendingAction = (id: number) =>
  fetchAPI<PendingAction>(`/copilot/actions/${id}/apply`, { method: 'POST' })

export const rejectPendingAction = (id: number) =>
  fetchAPI<PendingAction>(`/copilot/actions/${id}/reject`, { method: 'POST' })

/** 轨迹只读（P3）：回放与成本看板。 */
export interface CopilotRunRow {
  run_id: string
  conversation_id: number | null
  recipe: string | null
  status: string
  tool_calls: number
  elapsed_ms: number | null
  question: string | null
  created_at: string
}

export const getCopilotRuns = (limit = 20, signal?: AbortSignal) =>
  fetchAPI<CopilotRunRow[]>(`/copilot/runs?limit=${limit}`, { signal })
