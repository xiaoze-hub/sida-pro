import { fetchAPI } from './client'

export interface PremarketGate {
  score?: '强' | '中' | '弱' | string
  position_cap?: string
}

export interface PremarketMainline {
  name?: string
  strength?: string
}

export interface PremarketPlan {
  symbol?: string
  name?: string
  action?: 'watch' | 'hold' | 'alert' | 'avoid' | string
  trigger?: string
  stop?: string
}

export interface PremarketStructured {
  gate?: PremarketGate
  mainlines?: PremarketMainline[]
  plans?: PremarketPlan[]
}

export interface PremarketKeyLevel {
  symbol?: string
  kind?: 'support' | 'resistance' | 'cost' | string
  price?: number | string
}

export interface PremarketTodayResponse {
  date?: string
  empty?: boolean
  last?: {
    date?: string
  } | null
  as_of?: string
  is_stale?: boolean
  title?: string
  summary?: string
  structured?: PremarketStructured | null
  key_levels?: PremarketKeyLevel[]
  quality?: 'ok' | 'suspect' | 'missing' | string
}

export interface PremarketRunResponse {
  queued?: boolean
  message?: string
}

export interface PremarketEvidenceBlock {
  name?: string
  quality?: 'ok' | 'suspect' | 'missing' | string
  reason?: string
  items?: unknown[]
}

export interface PremarketEvidenceResponse {
  available?: boolean
  blocks?: PremarketEvidenceBlock[]
}

export const premarketApi = {
  getToday: (params?: { cacheMode?: 'reload' | number | false }) =>
    fetchAPI<PremarketTodayResponse>(
      '/premarket/today',
      params?.cacheMode !== undefined ? { cacheMode: params.cacheMode } : undefined,
    ),

  run: () =>
    fetchAPI<PremarketRunResponse>('/premarket/run', {
      method: 'POST',
    }),

  getEvidence: (date?: string) =>
    fetchAPI<PremarketEvidenceResponse>(
      `/premarket/evidence${date ? `?date=${encodeURIComponent(date)}` : ''}`,
    ),
}
