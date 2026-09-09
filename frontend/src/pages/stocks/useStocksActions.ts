import { useEffect } from 'react'
import type { Account } from './shared'
import type { AgentResult } from './shared'
import type { KlineSummary } from '@panwatch/biz-ui/components/suggestion-badge'
import type { Position } from './shared'
import type { SearchResult } from './shared'
import type { Stock } from './shared'
import type { SuggestionInfo } from '@panwatch/biz-ui/components/suggestion-badge'
import { buildKlineSuggestion } from '@/lib/kline-scorer'
import { emptyAccountForm } from './shared'
import { emptyStockForm } from './shared'
import { fetchAPI } from '@panwatch/api'
import { safeMoney } from '@/lib/format'
import { safePrice } from '@/lib/format'
import { stocksApi } from '@panwatch/api'
import { useToast } from '@panwatch/base-ui/components/ui/toast'

import type { useStocksState } from './useStocksState'
import type { useStocksData } from './useStocksData'

export function useStocksActions(
  state: ReturnType<typeof useStocksState>,
  data: ReturnType<typeof useStocksData>,
) {
  const {
    stocks,
    portfolio,
    quotes,
    klineSummaries,
    suggestions,
    poolSuggestions,
    priceAlertSummaryMap,
    setShowStockForm,
    stockForm,
    setStockForm,
    searchQuery,
    setSearchQuery,
    searchMarket,
    setSearchMarket,
    setSearchResults,
    setShowDropdown,
    setSearching,
    setRefreshingStockList,
    setAccountDialogOpen,
    accountForm,
    setAccountForm,
    editAccountId,
    setEditAccountId,
    setPositionDialogOpen,
    positionForm,
    setPositionForm,
    editPositionId,
    setEditPositionId,
    setPositionDialogAccountId,
    positionSearchQuery,
    setPositionSearchQuery,
    positionSearchMarket,
    setPositionSearchMarket,
    setPositionSearchResults,
    setPositionSearching,
    setShowPositionDropdown,
    positionSearchTimer,
    positionDropdownRef,
    setAgentDialogStock,
    setTriggeringAgent,
    setRunningAgents,
    setRemoveWatchStock,
    setRemovingWatchStock,
  } = state
  const {
    searchTimer,
    dropdownRef,
    load,
    loadPortfolio,
  } = data
  const { toast } = useToast()
useEffect(() => {
  const handler = (e: MouseEvent) => {
    if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
      setShowDropdown(false)
    }
    if (positionDropdownRef.current && !positionDropdownRef.current.contains(e.target as Node)) {
      setShowPositionDropdown(false)
    }
  }
  document.addEventListener('mousedown', handler)
  return () => document.removeEventListener('mousedown', handler)
}, [dropdownRef, positionDropdownRef, setShowDropdown, setShowPositionDropdown])

// ========== Stock handlers ==========
const doSearch = async (q: string, market: string = searchMarket) => {
  if (q.length < 1) { setSearchResults([]); setShowDropdown(false); return }
  setSearching(true)
  try {
    const marketParam = market ? `&market=${market}` : ''
    const results = await fetchAPI<SearchResult[]>(`/stocks/search?q=${encodeURIComponent(q)}${marketParam}`)
    setSearchResults(results)
    setShowDropdown(results.length > 0)
  } catch { setSearchResults([]) }
  finally { setSearching(false) }
}

const handleSearchInput = (value: string) => {
  setSearchQuery(value)
  clearTimeout(searchTimer.current)
  searchTimer.current = setTimeout(() => doSearch(value), 500)
}

const handleSearchMarketChange = (market: string) => {
  setSearchMarket(market)
  if (searchQuery) {
    doSearch(searchQuery, market)
  }
}

const refreshStockListCache = async () => {
  setRefreshingStockList(true)
  try {
    const result = await fetchAPI<{ count: number }>('/stocks/refresh-list', { method: 'POST' })
    toast(`已刷新股票列表，共 ${result.count} 只`, 'success')
    if (searchQuery) {
      doSearch(searchQuery)
    }
  } catch {
    toast('刷新失败', 'error')
  } finally {
    setRefreshingStockList(false)
  }
}

const selectStock = (item: SearchResult) => {
  setStockForm({ symbol: item.symbol, name: item.name, market: item.market })
  setSearchQuery(`${item.symbol} ${item.name}`)
  setShowDropdown(false)
}

const handleStockSubmit = async (e: React.FormEvent) => {
  e.preventDefault()
  try {
    await stocksApi.create(stockForm)
    setStockForm(emptyStockForm)
    setSearchQuery('')
    setShowStockForm(false)
    load()
    toast('股票已添加', 'success')
  } catch (e) {
    toast(e instanceof Error ? e.message : '添加股票失败', 'error')
  }
}

const hasAnyPositionForStockId = (id: number): boolean => {
  return (portfolio?.accounts || []).some(acc => (acc.positions || []).some(p => p.stock_id === id))
}

const removeFromWatchlist = async (stock: Stock) => {
  if (hasAnyPositionForStockId(stock.id)) {
    toast('该股票存在持仓，请先删除持仓后再删除股票', 'error')
    return
  }

  setRemovingWatchStock(true)
  try {
    await stocksApi.remove(stock.id)
    toast('股票已删除', 'success')
    setRemoveWatchStock(null)
    load()
    // 价格提醒/关联配置会随股票删除，刷新一次避免 UI 残留。
    loadPortfolio()
  } catch (e) {
    toast(e instanceof Error ? e.message : '删除失败', 'error')
  } finally {
    setRemovingWatchStock(false)
  }
}

// ========== Account handlers ==========
const openAccountDialog = (account?: Account) => {
  if (account) {
    setAccountForm({ name: account.name, available_funds: account.available_funds.toString() })
    setEditAccountId(account.id)
  } else {
    setAccountForm(emptyAccountForm)
    setEditAccountId(null)
  }
  setAccountDialogOpen(true)
}

const handleAccountSubmit = async () => {
  try {
    const payload = {
      name: accountForm.name,
      available_funds: parseFloat(accountForm.available_funds) || 0,
    }
    if (editAccountId) {
      await fetchAPI(`/accounts/${editAccountId}`, { method: 'PUT', body: JSON.stringify(payload) })
    } else {
      await fetchAPI('/accounts', { method: 'POST', body: JSON.stringify(payload) })
    }
    setAccountDialogOpen(false)
    load()
    loadPortfolio()
    toast(editAccountId ? '账户已更新' : '账户已创建', 'success')
  } catch (e) {
    toast(e instanceof Error ? e.message : '保存账户失败', 'error')
  }
}

const handleDeleteAccount = async (id: number) => {
  if (!confirm('确定删除该账户？这将同时删除该账户的所有持仓记录')) return
  try {
    await fetchAPI(`/accounts/${id}`, { method: 'DELETE' })
    load()
    loadPortfolio()
    toast('账户已删除', 'success')
  } catch (e) {
    toast(e instanceof Error ? e.message : '删除账户失败', 'error')
  }
}

// ========== Position handlers ==========
const openPositionDialog = (accountId: number, position?: Position) => {
  setPositionDialogAccountId(accountId)
  setPositionSearchQuery('')
  setPositionSearchResults([])
  setShowPositionDropdown(false)
  if (position) {
    setPositionForm({
      account_id: accountId,
      stock_id: position.stock_id,
      cost_price: position.cost_price.toString(),
      quantity: position.quantity.toString(),
      invested_amount: position.invested_amount?.toString() || '',
      trading_style: position.trading_style || '',
      stock_symbol: position.symbol,
      stock_name: position.name,
      stock_market: position.market,
    })
    setEditPositionId(position.id)
  } else {
    setPositionForm({
      account_id: accountId,
      stock_id: 0,
      cost_price: '',
      quantity: '',
      invested_amount: '',
      trading_style: '',
      stock_symbol: '',
      stock_name: '',
      stock_market: 'CN',
    })
    setEditPositionId(null)
  }
  setPositionDialogOpen(true)
}

const doPositionSearch = async (q: string, market: string = positionSearchMarket) => {
  if (q.length < 1) { setPositionSearchResults([]); setShowPositionDropdown(false); return }
  setPositionSearching(true)
  try {
    const marketParam = market ? `&market=${market}` : ''
    const results = await fetchAPI<SearchResult[]>(`/stocks/search?q=${encodeURIComponent(q)}${marketParam}`)
    setPositionSearchResults(results)
    setShowPositionDropdown(results.length > 0)
  } catch { setPositionSearchResults([]) }
  finally { setPositionSearching(false) }
}

const handlePositionSearchInput = (value: string) => {
  setPositionSearchQuery(value)
  clearTimeout(positionSearchTimer.current)
  positionSearchTimer.current = setTimeout(() => doPositionSearch(value), 500)
}

const handlePositionSearchMarketChange = (market: string) => {
  setPositionSearchMarket(market)
  if (positionSearchQuery) {
    doPositionSearch(positionSearchQuery, market)
  }
}

const selectPositionStock = (item: SearchResult) => {
  // 检查是否已有此股票
  const existing = stocks.find(s => s.symbol === item.symbol && s.market === item.market)
  setPositionForm({
    ...positionForm,
    stock_id: existing?.id || 0,
    stock_symbol: item.symbol,
    stock_name: item.name,
    stock_market: item.market,
  })
  setPositionSearchQuery(`${item.symbol} ${item.name}`)
  setShowPositionDropdown(false)
}

const handlePositionSubmit = async () => {
  try {
    let stockId = positionForm.stock_id

    // 如果是新增且股票不在自选中，先添加到自选
    if (!editPositionId && !stockId && positionForm.stock_symbol) {
      try {
        const newStock = await fetchAPI<Stock>('/stocks', {
          method: 'POST',
          body: JSON.stringify({
            symbol: positionForm.stock_symbol,
            name: positionForm.stock_name,
            market: positionForm.stock_market,
          })
        })
        stockId = newStock.id
        load() // 刷新股票列表
      } catch {
        // 股票可能已存在，尝试获取（兼容并发创建/历史数据）。
        try {
          const existingStocks = await fetchAPI<Stock[]>('/stocks')
          const existing = existingStocks.find(s => s.symbol === positionForm.stock_symbol && s.market === positionForm.stock_market)
          if (existing) {
            stockId = existing.id
          } else {
            toast('添加股票失败', 'error')
            return
          }
        } catch (e) {
          toast(e instanceof Error ? e.message : '添加股票失败', 'error')
          return
        }
      }
    }

    const payload = {
      account_id: positionForm.account_id,
      stock_id: stockId,
      cost_price: parseFloat(positionForm.cost_price),
      quantity: parseInt(positionForm.quantity),
      invested_amount: positionForm.invested_amount ? parseFloat(positionForm.invested_amount) : null,
      trading_style: positionForm.trading_style,  // 空字符串表示清空
    }
    if (editPositionId) {
      await fetchAPI(`/positions/${editPositionId}`, { method: 'PUT', body: JSON.stringify(payload) })
    } else {
      await fetchAPI('/positions', { method: 'POST', body: JSON.stringify(payload) })
    }
    setPositionDialogOpen(false)
    loadPortfolio()
    toast(editPositionId ? '持仓已更新' : '持仓已添加', 'success')
  } catch (e) {
    toast(e instanceof Error ? e.message : '保存持仓失败', 'error')
  }
}

const handleDeletePosition = async (id: number) => {
  if (!confirm('确定删除该持仓？')) return
  try {
    await fetchAPI(`/positions/${id}`, { method: 'DELETE' })
    loadPortfolio()
    toast('持仓已删除', 'success')
  } catch (e) {
    toast(e instanceof Error ? e.message : '删除持仓失败', 'error')
  }
}

// ========== Agent handlers ==========
const toggleAgent = async (stock: Stock, agentName: string) => {
  try {
    const current = stock.agents || []
    const isAssigned = current.some(a => a.agent_name === agentName)
    const newAgents = isAssigned
      ? current.filter(a => a.agent_name !== agentName)
      : [...current, { agent_name: agentName, schedule: '', ai_model_id: null, notify_channel_ids: [] }]
    await fetchAPI(`/stocks/${stock.id}/agents`, { method: 'PUT', body: JSON.stringify({ agents: newAgents }) })
    load()
    setAgentDialogStock(prev => prev ? { ...prev, agents: newAgents } : null)
  } catch (e) {
    toast(e instanceof Error ? e.message : '更新 Agent 绑定失败', 'error')
  }
}

const triggerStockAgent = async (stockId: number, agentName: string) => {
  setTriggeringAgent(agentName)
  setRunningAgents(prev => ({ ...prev, [stockId]: agentName }))
  // 触发后立即关闭配置弹窗，避免多层弹窗干扰
  setAgentDialogStock(null)
  try {
    // 手动触发时跳过节流，方便测试
    const resp = await fetchAPI<{ result: AgentResult; success?: boolean; message?: string }>(
      `/stocks/${stockId}/agents/${agentName}/trigger?bypass_throttle=true`,
      { method: 'POST' }
    )
    const result = resp?.result
    if (result) {
      // 仅提示，不再弹出结果弹窗，避免干扰
      if (result.success === false) {
        toast(result.message || result.content || '执行未通过', 'info')
        return
      }
      const isSkipped = !!result.skipped || /已跳过执行|非交易时段/.test(result.content || '')
      if (isSkipped) {
        toast(result.content || '当前非交易时段，已跳过执行', 'info')
      } else {
        toast(result.should_alert ? 'AI 建议关注' : 'AI 判断无需关注', result.should_alert ? 'success' : 'info')
      }
    }
  } catch (e) {
    const msg = e instanceof Error ? e.message : '触发失败'
    if (/非交易时段|跳过执行/.test(msg)) {
      toast(msg, 'info')
    } else {
      toast(msg, 'error')
    }
  } finally {
    setTriggeringAgent(null)
    setRunningAgents(prev => ({ ...prev, [stockId]: null }))
  }
}

const updateStockAgentModel = async (stock: Stock, agentName: string, modelId: number | null) => {
  try {
    const newAgents = (stock.agents || []).map(a =>
      a.agent_name === agentName ? { ...a, ai_model_id: modelId } : a
    )
    await fetchAPI(`/stocks/${stock.id}/agents`, { method: 'PUT', body: JSON.stringify({ agents: newAgents }) })
    load()
    setAgentDialogStock(prev => prev ? { ...prev, agents: newAgents } : null)
  } catch (e) {
    toast(e instanceof Error ? e.message : '更新 Agent 模型失败', 'error')
  }
}

const toggleStockAgentChannel = async (stock: Stock, agentName: string, channelId: number) => {
  try {
    const newAgents = (stock.agents || []).map(a => {
      if (a.agent_name !== agentName) return a
      const current = a.notify_channel_ids || []
      const newIds = current.includes(channelId)
        ? current.filter(id => id !== channelId)
        : [...current, channelId]
      return { ...a, notify_channel_ids: newIds }
    })
    await fetchAPI(`/stocks/${stock.id}/agents`, { method: 'PUT', body: JSON.stringify({ agents: newAgents }) })
    load()
    setAgentDialogStock(prev => prev ? { ...prev, agents: newAgents } : null)
  } catch (e) {
    toast(e instanceof Error ? e.message : '更新 Agent 通知配置失败', 'error')
  }
}

const updateStockAgentSchedule = async (stock: Stock, agentName: string, schedule: string) => {
  try {
    const newAgents = (stock.agents || []).map(a =>
      a.agent_name === agentName ? { ...a, schedule } : a
    )
    await fetchAPI(`/stocks/${stock.id}/agents`, { method: 'PUT', body: JSON.stringify({ agents: newAgents }) })
    load()
    setAgentDialogStock(prev => prev ? { ...prev, agents: newAgents } : null)
  } catch (e) {
    toast(e instanceof Error ? e.message : '更新 Agent 调度失败', 'error')
  }
}

// ========== Helpers ==========
// 修复(S-5, 2026-08-23): PG DECIMAL 经 psycopg2 后变字符串, 裸 .toFixed 抛 TypeError.
// 改走 lib/format.safeMoney (null/NaN/字符串数字都走 fallback).
const formatMoney = (value: unknown) => safeMoney(value)

const marketLabel = (m: string) => m === 'CN' ? 'A股' : m === 'HK' ? '港股' : m === 'US' ? '美股' : m

// 市场徽章样式和短标签
const marketBadge = (m: string) => {
  if (m === 'HK') return { style: 'bg-orange-500/10 text-orange-600', label: '港' }
  if (m === 'US') return { style: 'bg-green-500/10 text-green-600', label: '美' }
  return { style: 'bg-blue-500/10 text-blue-600', label: 'A' }
}

// 修复(S-5, 2026-08-23): 同 formatMoney, 价格字段也走 safe wrapper.
const formatPrice = (value: unknown) => safePrice(value)

// 获取股票的行情信息
const getStockQuote = (quoteKey: string) => {
  return quotes[quoteKey] || null
}

const getPriceAlertSummary = (symbol: string, market: string) => {
  const key = `${String(market || 'CN').toUpperCase()}:${String(symbol || '').toUpperCase()}`
  return priceAlertSummaryMap[key] || { total: 0, enabled: 0 }
}

// 获取股票的建议信息（优先使用建议池，包含来源和时间信息）
const getSuggestionForStock = (symbol: string, market: string, hasPosition?: boolean): { suggestion: SuggestionInfo | null; kline: KlineSummary | null } => {
  const key = `${market || 'CN'}:${symbol}`
  // 优先使用建议池的建议（包含来源和时间信息）
  const poolSug =
    poolSuggestions[key] ||
    (() => {
      const fallback = poolSuggestions[symbol]
      if (!fallback) return null
      const fm = String(fallback.stock_market || '').toUpperCase()
      return fm && fm !== String(market || 'CN').toUpperCase() ? null : fallback
    })()
  if (poolSug) {
    const preloadedKline = klineSummaries[key] || (suggestions[symbol]?.kline as any) || null
    return {
      suggestion: {
        id: poolSug.id,
        action: poolSug.action,
        action_label: poolSug.action_label,
        signal: poolSug.signal,
        reason: poolSug.reason,
        should_alert: poolSug.should_alert ?? (['alert', 'avoid', 'sell', 'reduce'].includes(poolSug.action)),
        agent_name: poolSug.agent_name,
        agent_label: poolSug.agent_label,
        created_at: poolSug.created_at,
        is_expired: poolSug.is_expired,
        prompt_context: poolSug.prompt_context,
        ai_response: poolSug.ai_response,
        meta: poolSug.meta,
      },
      // 优先使用本页并发预取的 kline 摘要，确保徽章与弹窗一致且免加载
      kline: preloadedKline,
    }
  }

  // 无池建议时，使用 K 线评分构建轻量建议（仅用于徽章展示）
  const ks = klineSummaries[key]
  if (ks) {
    const scored = buildKlineSuggestion(ks as any, hasPosition)
    return {
      suggestion: {
        action: scored.action,
        action_label: scored.action_label,
        signal: scored.signal,
        reason: '',
        should_alert: false,
        agent_label: '技术指标',
      },
      kline: ks,
    }
  }

  return { suggestion: null, kline: null }
}


  return {
    handleSearchInput,
    handleSearchMarketChange,
    refreshStockListCache,
    selectStock,
    handleStockSubmit,
    hasAnyPositionForStockId,
    removeFromWatchlist,
    openAccountDialog,
    handleAccountSubmit,
    handleDeleteAccount,
    openPositionDialog,
    handlePositionSearchInput,
    handlePositionSearchMarketChange,
    selectPositionStock,
    handlePositionSubmit,
    handleDeletePosition,
    toggleAgent,
    triggerStockAgent,
    updateStockAgentModel,
    toggleStockAgentChannel,
    updateStockAgentSchedule,
    formatMoney,
    marketLabel,
    marketBadge,
    formatPrice,
    getStockQuote,
    getPriceAlertSummary,
    getSuggestionForStock,
  }
}
