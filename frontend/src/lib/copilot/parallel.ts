/** 并行诊断接口（工作台·多子 run）。走 fetchAPI，自带 /api 前缀。 */
import { fetchAPI } from '@sida/api'

export interface ParallelChild {
  run_id: string
  recipe_id: string | null
  status: string
  tool_calls: number | null
  elapsed_ms: number | null
  error: string | null
  question: string
}

export interface ParallelSummary {
  available: boolean
  reason: string
  children: ParallelChild[]
  count: number
  ok: number
  failed: number
  pending?: number
  tool_calls: number
  gaps: string[]
}

export interface ParallelState {
  run_id: string
  status: string
  error: string | null
  created_at: string
  summary: ParallelSummary
}

export const submitParallel = (questions: string[]) =>
  fetchAPI<{ run_id: string; status: string; count: number }>('/copilot/parallel', {
    method: 'POST',
    body: JSON.stringify({ items: questions.map((q) => ({ question: q })) }),
    headers: { 'Content-Type': 'application/json' },
  } as RequestInit)

export const getParallel = (runId: string, signal?: AbortSignal) =>
  fetchAPI<ParallelState>(`/copilot/parallel/${encodeURIComponent(runId)}`, { signal })
