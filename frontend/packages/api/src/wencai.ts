import { fetchAPI } from './client'

export interface WencaiRow {
  symbol: string
  name: string
  [key: string]: string | number | null
}

export interface WencaiResponse {
  available: boolean
  rows: WencaiRow[]
  note: string
}

export const wencaiApi = {
  /** 同花顺问财自然语言选股(需 L2 数据源, available=false 表示未接入) */
  query: (q: string, timeoutMs = 30000) =>
    fetchAPI<WencaiResponse>(`/wencai?query=${encodeURIComponent(q)}`, {
      cacheMode: false,
      timeoutMs,
    }),
}
