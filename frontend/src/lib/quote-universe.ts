import { dashboardApi, stocksApi } from '@sida/api'

export interface QuoteChoice { symbol: string; market: string; name: string; source: 'position' | 'watchlist' }
export interface QuoteUniverse { positions: QuoteChoice[]; watchlist: QuoteChoice[]; errors: string[] }
export const quoteChoiceKey = (choice: Pick<QuoteChoice, 'market' | 'symbol'>) => `${choice.market}:${choice.symbol}`
export function quotePath(choice: Pick<QuoteChoice, 'market' | 'symbol'>) {
  return `/stocks/${encodeURIComponent(choice.symbol)}?type=stock&market=${encodeURIComponent(choice.market)}`
}
function selectionKey() {
  try { const id = JSON.parse(localStorage.getItem('user') || '{}').id; return id ? `sida_quote_selection:${id}` : null } catch { return null }
}
export function rememberQuote(choice: Pick<QuoteChoice, 'market' | 'symbol'>) {
  try { const key = selectionKey(); if (key) sessionStorage.setItem(key, quoteChoiceKey(choice)) } catch { /* 无存储时仍能切换 */ }
}
export function preferredQuote(universe: QuoteUniverse): QuoteChoice | null {
  const choices = [...universe.positions, ...universe.watchlist]
  let last: string | null = null
  try { const key = selectionKey(); last = key ? sessionStorage.getItem(key) : null } catch { /* 使用列表默认顺序 */ }
  return choices.find(choice => quoteChoiceKey(choice) === last) ?? choices[0] ?? null
}
export async function loadQuoteUniverse(): Promise<QuoteUniverse> {
  const results = await Promise.allSettled([
    Promise.resolve().then(() => dashboardApi.portfolioSummary({ include_quotes: false })),
    Promise.resolve().then(() => stocksApi.list()),
  ])
  const positions: QuoteChoice[] = []
  const watchlist: QuoteChoice[] = []
  const seen = new Set<string>()
  const add = (target: QuoteChoice[], value: { symbol: string; name?: string; market: string }, source: QuoteChoice['source']) => {
    if (!value.symbol || !['CN', 'HK', 'US'].includes(value.market)) return
    const key = quoteChoiceKey(value)
    if (seen.has(key)) return
    seen.add(key)
    target.push({ symbol: value.symbol, market: value.market, name: value.name || value.symbol, source })
  }
  const portfolio = results[0]
  if (portfolio.status === 'fulfilled') for (const account of portfolio.value.accounts ?? []) for (const position of account.positions ?? []) {
    if (position.quantity > 0) add(positions, position, 'position')
  }
  const stocks = results[1]
  if (stocks.status === 'fulfilled') for (const stock of stocks.value ?? []) add(watchlist, stock, 'watchlist')
  const errors = results.flatMap((result, index) => result.status === 'rejected' ? [index === 0 ? '持仓读取失败' : '关注列表读取失败'] : [])
  return { positions, watchlist, errors }
}
