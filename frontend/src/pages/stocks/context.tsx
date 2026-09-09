import { createContext, useContext } from 'react'
import type { useStocksState } from './useStocksState'
import type { useStocksData } from './useStocksData'
import type { useStocksDerived } from './useStocksDerived'
import type { useStocksActions } from './useStocksActions'

export type StocksCtx = ReturnType<typeof useStocksState> &
  ReturnType<typeof useStocksData> &
  ReturnType<typeof useStocksDerived> &
  ReturnType<typeof useStocksActions>

export const StocksContext = createContext<StocksCtx | null>(null)

export function useStocks(): StocksCtx {
  const ctx = useContext(StocksContext)
  if (!ctx) throw new Error('useStocks must be used inside StocksPage')
  return ctx
}
