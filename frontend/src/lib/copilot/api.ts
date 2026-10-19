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
