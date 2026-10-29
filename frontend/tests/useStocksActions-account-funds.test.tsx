// @vitest-environment jsdom
/**
 * 账户现金未知（available_funds = NULL）· 行为回归（2026-10-29 安全修复 8db9088b 契约）。
 *
 * 后端口径：`available_funds` 为 NULL 表示「从未登记过现金」= **未知**，
 * 不是 0；响应里带 `available_funds_known=false`，总资产 basis 为
 * `available_funds_unknown`，且 `total_assets=null`。
 *
 * 两个前端出口在这条契约下会当场坏掉：
 *  ① `openAccountDialog` 里 `account.available_funds.toString()` —— null 直接 TypeError，
 *     点「编辑账户」就崩（红测复现）；
 *  ② `handleAccountSubmit` 里 `parseFloat(...) || 0` —— 空/坏值被静默写成 0 元现金，
 *     等于把一个「不知道」伪造成「账上真的一分钱没有」。
 */
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ fetchAPI: vi.fn(), stocksApi: {} }))
const toastMock = vi.hoisted(() => ({ toast: vi.fn() }))

vi.mock('@sida/api', () => ({ fetchAPI: api.fetchAPI, stocksApi: api.stocksApi }))
vi.mock('@sida/base-ui/components/ui/toast', () => ({ useToast: () => toastMock }))

import { useStocksActions } from '@/pages/stocks/useStocksActions'

type AccountLite = {
  id: number
  name: string
  available_funds: number | null
  enabled?: boolean
}

/** 只搭这条链路上真正用到的 state/data 字段；其余留空（未使用）。 */
function harness(over: Record<string, unknown> = {}) {
  const setAccountForm = vi.fn()
  const setEditAccountId = vi.fn()
  const setAccountDialogOpen = vi.fn()
  const state: Record<string, unknown> = {
    stocks: [],
    portfolio: null,
    quotes: {},
    klineSummaries: {},
    suggestions: {},
    poolSuggestions: [],
    priceAlertSummaryMap: {},
    showStockForm: false,
    stockForm: { symbol: '', name: '', market: 'CN' },
    searchQuery: '',
    searchMarket: 'CN',
    searchResults: [],
    showDropdown: false,
    searching: false,
    refreshingStockList: false,
    accountDialogOpen: false,
    accountForm: { name: '', available_funds: '0' },
    editAccountId: null,
    positionDialogOpen: false,
    positionForm: {},
    editPositionId: null,
    positionDialogAccountId: null,
    positionSearchQuery: '',
    positionSearchMarket: 'CN',
    positionSearchResults: [],
    positionSearching: false,
    setShowStockForm: vi.fn(),
    setStockForm: vi.fn(),
    setSearchQuery: vi.fn(),
    setSearchMarket: vi.fn(),
    setSearchResults: vi.fn(),
    setShowDropdown: vi.fn(),
    setSearching: vi.fn(),
    setRefreshingStockList: vi.fn(),
    setAccountDialogOpen,
    setAccountForm,
    setEditAccountId,
    setPositionDialogOpen: vi.fn(),
    setPositionForm: vi.fn(),
    setEditPositionId: vi.fn(),
    setPositionDialogAccountId: vi.fn(),
    setPositionSearchQuery: vi.fn(),
    setPositionSearchMarket: vi.fn(),
    setPositionSearchResults: vi.fn(),
    setPositionSearching: vi.fn(),
    ...over,
  }
  const data: Record<string, unknown> = {
    load: vi.fn(),
    loadPortfolio: vi.fn(),
    getSuggestionForStock: () => ({}),
  }
  const { result } = renderHook(() =>
    useStocksActions(
      state as unknown as Parameters<typeof useStocksActions>[0],
      data as unknown as Parameters<typeof useStocksActions>[1],
    ),
  )
  return { result, state, data, setAccountForm, setEditAccountId, setAccountDialogOpen }
}

beforeEach(() => {
  api.fetchAPI.mockReset()
  toastMock.toast.mockReset()
})

afterEach(() => cleanupAll())

function cleanupAll() {
  document.body.innerHTML = ''
}

describe('openAccountDialog · 现金未知时不崩', () => {
  it('available_funds 为 null → 表单填空串（旧实现 .toString() 直接 TypeError）', () => {
    const h = harness()
    const account = { id: 7, name: '老账户', available_funds: null, enabled: true } as AccountLite
    expect(() =>
      act(() => {
        h.result.current.openAccountDialog(
          account as unknown as Parameters<typeof h.result.current.openAccountDialog>[0],
        )
      }),
    ).not.toThrow()
    expect(h.setAccountForm).toHaveBeenCalledWith({ name: '老账户', available_funds: '' })
    expect(h.setEditAccountId).toHaveBeenLastCalledWith(7)
  })

  it('available_funds 有值时照常显示数字（不把它变成空串）', () => {
    const h = harness()
    const account = { id: 8, name: '新账户', available_funds: 12345.6, enabled: true } as AccountLite
    act(() => {
      h.result.current.openAccountDialog(
        account as unknown as Parameters<typeof h.result.current.openAccountDialog>[0],
      )
    })
    expect(h.setAccountForm).toHaveBeenCalledWith({ name: '新账户', available_funds: '12345.6' })
  })

  it('available_funds = 0 是真实数值，不能被当成「未知」显示成空', () => {
    const h = harness()
    const account = { id: 9, name: '空仓账户', available_funds: 0, enabled: true } as AccountLite
    act(() => {
      h.result.current.openAccountDialog(
        account as unknown as Parameters<typeof h.result.current.openAccountDialog>[0],
      )
    })
    expect(h.setAccountForm).toHaveBeenCalledWith({ name: '空仓账户', available_funds: '0' })
  })
})

describe('handleAccountSubmit · 未知现金不许被伪造成 0', () => {
  it('编辑账户且现金留空 → 请求体不带 available_funds（不写 0，也不清空已知值）', async () => {
    api.fetchAPI.mockResolvedValue({})
    const h = harness({
      editAccountId: 7,
      accountForm: { name: '老账户', available_funds: '' },
    })
    await act(async () => {
      await h.result.current.handleAccountSubmit()
    })
    expect(api.fetchAPI).toHaveBeenCalledTimes(1)
    const [url, init] = api.fetchAPI.mock.calls[0] as [string, { method: string; body: string }]
    expect(url).toBe('/accounts/7')
    expect(init.method).toBe('PUT')
    const body = JSON.parse(init.body)
    expect(body).toEqual({ name: '老账户' })
    expect('available_funds' in body).toBe(false)
  })

  it('填了数字 → 原样提交该数字', async () => {
    api.fetchAPI.mockResolvedValue({})
    const h = harness({
      editAccountId: 7,
      accountForm: { name: '老账户', available_funds: '5000.5' },
    })
    await act(async () => {
      await h.result.current.handleAccountSubmit()
    })
    const [, init] = api.fetchAPI.mock.calls[0] as [string, { body: string }]
    expect(JSON.parse(init.body)).toEqual({ name: '老账户', available_funds: 5000.5 })
  })

  it('填了 0 → 提交 0（真实值，不是「未知」）', async () => {
    api.fetchAPI.mockResolvedValue({})
    const h = harness({ editAccountId: 7, accountForm: { name: '老账户', available_funds: '0' } })
    await act(async () => {
      await h.result.current.handleAccountSubmit()
    })
    const [, init] = api.fetchAPI.mock.calls[0] as [string, { body: string }]
    expect(JSON.parse(init.body)).toEqual({ name: '老账户', available_funds: 0 })
  })

  it('填了非数字 → 报错且不发请求（旧实现 parseFloat(...) || 0 会静默写成 0 元）', async () => {
    const h = harness({
      editAccountId: 7,
      accountForm: { name: '老账户', available_funds: 'abc' },
    })
    await act(async () => {
      await h.result.current.handleAccountSubmit()
    })
    expect(api.fetchAPI).not.toHaveBeenCalled()
    expect(toastMock.toast).toHaveBeenCalled()
  })
})
