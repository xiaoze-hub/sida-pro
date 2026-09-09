import { createContext, useContext } from 'react'
import type { useInsightData } from './useInsightData'
import type { useInsightDerived } from './useInsightDerived'
import type { useInsightActions } from './useInsightActions'
import type { StockInsightModalProps } from './types'

export type InsightCtx = { props: StockInsightModalProps } & ReturnType<typeof useInsightData> &
  ReturnType<typeof useInsightDerived> &
  ReturnType<typeof useInsightActions>

export const InsightContext = createContext<InsightCtx | null>(null)

export function useInsight(): InsightCtx {
  const ctx = useContext(InsightContext)
  if (!ctx) throw new Error('useInsight must be used inside StockInsightModal')
  return ctx
}
