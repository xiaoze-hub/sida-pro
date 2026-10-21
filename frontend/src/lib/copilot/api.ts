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
  /** 该轮 token 合计（llm_usage 按 run_id 汇总；无记录为 null） */
  tokens?: number | null
  question: string | null
  created_at: string
}

export const getCopilotRuns = (limit = 20, signal?: AbortSignal) =>
  fetchAPI<CopilotRunRow[]>(`/copilot/runs?limit=${limit}`, { signal })


export interface CopilotMetrics {
  days: number
  runs: number
  ok_rate: number | null
  avg_tools: number | null
  p95_elapsed_ms: number | null
  by_recipe: { recipe: string; runs: number }[]
}

export const getCopilotMetrics = (days = 7, signal?: AbortSignal) =>
  fetchAPI<CopilotMetrics>(`/copilot/metrics?days=${days}`, { signal })


/** 单轮轨迹回放（P3）：骨架 + 每步工具调用。 */
export interface CopilotRunStep {
  seq: number
  tool: string
  ok: boolean
  elapsed_ms: number | null
  args: string | null
  result_digest: string | null
}

export interface CopilotRunDetail {
  run: {
    run_id: string
    status: string
    recipe: string | null
    question: string | null
    tool_calls: number
    elapsed_ms: number | null
    created_at: string
  }
  steps: CopilotRunStep[]
}

export const getCopilotRunDetail = (runId: string, signal?: AbortSignal) =>
  fetchAPI<CopilotRunDetail>(`/copilot/runs/${encodeURIComponent(runId)}`, { signal })

/** 成本看板（P3）：按日/按模型/按用户聚合 token 与估算费用。 */
export interface CopilotCostBucket {
  date?: string
  model?: string
  user_id?: string
  tokens: number
  calls: number
  cost: number | null
}

export interface CopilotCost {
  days: number
  total_tokens: number
  total_calls: number
  by_day: CopilotCostBucket[]
  by_model: CopilotCostBucket[]
  by_user: CopilotCostBucket[]
  unpriced_models: string[]
  scope?: string
}

export const getCopilotCost = (days = 7, signal?: AbortSignal) =>
  fetchAPI<CopilotCost>(`/copilot/governance/cost?days=${days}`, { signal })

/** 多用户配额（P3）：没有配额行 = 不限；超限网关层直接 429。 */
export interface CopilotQuotaRow {
  user_id: string
  daily_runs: number | null
  daily_tokens: number | null
  enabled: boolean
  note?: string | null
  used_runs?: number | null
  used_tokens?: number | null
  updated_at?: string
}

export const getCopilotQuotas = (signal?: AbortSignal) =>
  fetchAPI<{ scope: string; items: CopilotQuotaRow[] }>('/copilot/governance/quota', { signal })

export const putCopilotQuota = (
  userId: string,
  body: { daily_runs: number | null; daily_tokens: number | null; enabled: boolean; note?: string },
) =>
  fetchAPI<CopilotQuotaRow>(`/copilot/governance/quota/${encodeURIComponent(userId)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })


/** 长期记忆（P0）：用户明确表达的偏好/规则，换会话也记得。 */
export interface CopilotMemoryRow {
  id: number
  kind: 'preference' | 'rule' | 'fact' | 'skill'
  content: string
  weight: number
  source: string
  enabled: boolean
  hits: number
  created_at: string
}

export const getMemories = (signal?: AbortSignal) =>
  fetchAPI<CopilotMemoryRow[]>('/copilot/memory', { signal })

export const postMemory = (content: string, kind = 'preference', weight = 5) =>
  fetchAPI<CopilotMemoryRow>('/copilot/memory', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, kind, weight }),
  })

export const patchMemory = (id: number, enabled: boolean) =>
  fetchAPI<{ ok: boolean }>(`/copilot/memory/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  })

export const deleteMemory = (id: number) =>
  fetchAPI<{ ok: boolean }>(`/copilot/memory/${id}`, { method: 'DELETE' })

/** 会话定时任务（P0）：到点把内容写回会话。 */
export interface CopilotScheduleRow {
  id: number
  kind: 'auction_digest' | 'daily_brief'
  at_hhmm: string
  weekdays: string
  conversation_id: number | null
  enabled: boolean
  last_run_date: string | null
  created_at: string
}

export const getSchedules = (signal?: AbortSignal) =>
  fetchAPI<CopilotScheduleRow[]>('/copilot/schedules', { signal })

export const postSchedule = (kind: string, at_hhmm: string, weekdays = 'mon-fri') =>
  fetchAPI<CopilotScheduleRow>('/copilot/schedules', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ kind, at_hhmm, weekdays }),
  })

export const patchSchedule = (id: number, enabled: boolean) =>
  fetchAPI<{ ok: boolean }>(`/copilot/schedules/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  })

export const deleteSchedule = (id: number) =>
  fetchAPI<{ ok: boolean }>(`/copilot/schedules/${id}`, { method: 'DELETE' })
