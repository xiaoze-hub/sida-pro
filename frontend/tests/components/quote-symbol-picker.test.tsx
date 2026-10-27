// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
const mocks = vi.hoisted(() => ({ portfolio: vi.fn(), stocks: vi.fn() }))
vi.mock('@sida/api', () => ({ dashboardApi: { portfolioSummary: mocks.portfolio }, stocksApi: { list: mocks.stocks } }))
import { loadQuoteUniverse, preferredQuote, quotePath, rememberQuote } from '@/lib/quote-universe'
import { QuoteSymbolPicker } from '@/components/QuoteSymbolPicker'
import QuoteEntry from '@/pages/QuoteEntry'

function Location() { const location = useLocation(); return <output data-testid="path">{location.pathname + location.search}</output> }
beforeEach(() => {
  localStorage.clear(); sessionStorage.clear()
  mocks.portfolio.mockResolvedValue({ accounts: [{ positions: [{ symbol: '600111', name: '北方稀土', market: 'CN', quantity: 100 }] }] })
  mocks.stocks.mockResolvedValue([{ symbol: '600111', name: '北方稀土', market: 'CN' }, { symbol: '00700', name: '腾讯控股', market: 'HK' }])
})
afterEach(cleanup)
it('持仓优先且按市场和代码去重，保留外盘市场', async () => {
  const universe = await loadQuoteUniverse()
  expect(universe.positions.map(item => item.symbol)).toEqual(['600111'])
  expect(universe.watchlist.map(item => item.symbol)).toEqual(['00700'])
  expect(preferredQuote(universe)?.source).toBe('position')
  expect(quotePath(universe.watchlist[0])).toBe('/stocks/00700?type=stock&market=HK')
})
it('上次选择仅在仍属于当前账号列表时恢复，账号间隔离', async () => {
  const universe = await loadQuoteUniverse()
  localStorage.setItem('user', JSON.stringify({ id: 'one' }))
  rememberQuote(universe.watchlist[0])
  expect(preferredQuote(universe)?.symbol).toBe('00700')
  for (const id of ['two', 'three', 'four']) {
    localStorage.setItem('user', JSON.stringify({ id }))
    expect(preferredQuote(universe)?.symbol).toBe('600111')
  }
})
it('行情入口自动打开持仓，没有持仓/关注才回到默认指数', async () => {
  const view = render(<MemoryRouter initialEntries={['/quotes']}><Location /><Routes><Route path="/quotes" element={<QuoteEntry />} /><Route path="/stocks/:symbol" element={<div>行情正文</div>} /></Routes></MemoryRouter>)
  await waitFor(() => expect(screen.getByTestId('path').textContent).toBe('/stocks/600111?type=stock&market=CN'))
  view.unmount()
  mocks.portfolio.mockResolvedValue({ accounts: [] }); mocks.stocks.mockResolvedValue([])
  render(<MemoryRouter initialEntries={['/quotes']}><Location /><Routes><Route path="/quotes" element={<QuoteEntry />} /><Route path="/stocks/:symbol" element={<div>行情正文</div>} /></Routes></MemoryRouter>)
  await waitFor(() => expect(screen.getByTestId('path').textContent).toBe('/stocks/000001?type=index&default=1'))
})
it('读取失败不能伪装没有关注，也不自动跳到平安银行', async () => {
  mocks.portfolio.mockRejectedValue(new Error('offline')); mocks.stocks.mockRejectedValue(new Error('offline'))
  render(<MemoryRouter initialEntries={['/quotes']}><Location /><QuoteEntry /></MemoryRouter>)
  await screen.findByRole('alert')
  expect(screen.getByTestId('path').textContent).toBe('/quotes')
  expect(screen.getByRole('button', { name: '重新读取' })).toBeTruthy()
})
it('切换器可搜索关注股，按所选市场导航', async () => {
  render(<MemoryRouter><Location /><QuoteSymbolPicker symbol="000001" market="CN" type="index" defaultIndex={false} /></MemoryRouter>)
  fireEvent.click(screen.getByTestId('quote-symbol-picker'))
  fireEvent.change(await screen.findByRole('textbox', { name: '搜索持仓与关注股票' }), { target: { value: '腾讯' } })
  fireEvent.click(await screen.findByRole('button', { name: /腾讯控股/ }))
  expect(screen.getByTestId('path').textContent).toBe('/stocks/00700?type=stock&market=HK')
})
