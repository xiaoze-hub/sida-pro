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
