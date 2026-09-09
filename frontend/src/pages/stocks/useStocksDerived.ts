import { useMemo } from 'react'
import { getToken } from '@panwatch/api'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

import type { useStocksState } from './useStocksState'
import type { useStocksData } from './useStocksData'

export function useStocksDerived(
  state: ReturnType<typeof useStocksState>,
  data: ReturnType<typeof useStocksData>,
) {
  const {
    stocks,
    portfolio,
    setExpandedAccounts,
    marketStatus,
  } = state
  const {

  } = data
  const { toast } = useToast()
const positionRatio = useMemo(() => {
  if (!portfolio) return null
  const mv = portfolio.total.total_market_value || 0
  const assets = portfolio.total.total_assets || 0
  const pct = assets > 0 ? (mv / assets * 100) : 0
  return { mv, assets, pct }
}, [portfolio])

const portfolioMarketStatusLabel = useMemo(() => {
  const heldMarkets = new Set(
    (portfolio?.accounts || []).flatMap(account =>
      (account.positions || []).map(position => position.market),
    ),
  )
  return marketStatus
    .filter(item => heldMarkets.has(item.code))
    .map(item => `${item.name} · ${item.status_text}`)
    .join(' / ')
}, [marketStatus, portfolio])

const positionsCount = useMemo(() => {
  return (portfolio?.accounts || []).reduce((acc, a) => acc + (a.positions?.length || 0), 0)
}, [portfolio])

const watchlistCount = useMemo(() => {
  return stocks.length
}, [stocks])

const toggleAccountExpanded = (id: number) => {
  setExpandedAccounts(prev => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })
}

// 骨架屏：初始加载时显示

const exportPortfolio = async () => {
  try {
    const token = getToken()
    const res = await fetch('/api/export/portfolio', {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `持仓_${new Date().toISOString().slice(0, 10)}.csv`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
    toast('持仓已导出', 'success')
  } catch (e) {
    toast(e instanceof Error ? e.message : '导出失败', 'error')
  }
}

  return {
    positionRatio,
    portfolioMarketStatusLabel,
    positionsCount,
    watchlistCount,
    toggleAccountExpanded,
    exportPortfolio,
  }
}
