// @vitest-environment jsdom
//
// perf(2026-10-05) 回归钉: 带1 `HeaderBand` 的**快慢车道拆分**(B1 首屏冷启动)。
//
// 背景(B1 走查 `/stocks/002361` 冷态 settle ~22.7s): 慢接口 `/klines/{s}/summary`(冷链 ~10s+)
// 此前与行情塞进同一个 `Promise.allSettled` ⇒ 顶行/快照行早已落位, 刷新时代的转圈图标却要一直
// 转到摘要回来(首屏像卡住)。现在:
//  ① `busy`(刷新转圈)只跟**快车道**(quote/more-info/l2) —— 摘要在途也不拖累"已出壳"观感;
//  ② 摘要走**慢车道**: 错峰(首帧之后才发)+ 显式 45s 超时(默认 20s 会在冷启动窗口提前掐断);
//  ③ 摘要失败 → 显式降级(不出建议条), 不编造。
//
// 真组件 + mock 网络层(`@panwatch/api`), 组件取数/渲染走真实代码。
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  fetchAPI: vi.fn(),
  quote: vi.fn(),
  moreInfo: vi.fn(),
  klineSummary: vi.fn(),
}))

vi.mock('@panwatch/api', () => ({
  fetchAPI: (...args: unknown[]) => mocks.fetchAPI(...args),
  insightApi: {
    quote: (...args: unknown[]) => mocks.quote(...args),
    moreInfo: (...args: unknown[]) => mocks.moreInfo(...args),
    klineSummary: (...args: unknown[]) => mocks.klineSummary(...args),
  },
}))

import HeaderBand from '@panwatch/biz-ui/components/workbench/HeaderBand'

type Deferred = { promise: Promise<unknown>; resolve: (v: unknown) => void; reject: (e: unknown) => void }
function defer(): Deferred {
  let resolve!: (v: unknown) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<unknown>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const QUOTE = { name: '平安银行', current_price: 11.74, change_pct: -0.93, open_price: 11.82, high_price: 11.86 }

/** 刷新按钮里那个刷新图标 svg(其 className 上挂 `animate-spin`)。 */
const refreshIconClass = () =>
  screen.getByRole('button', { name: '刷新' }).querySelector('svg')?.getAttribute('class') ?? ''

beforeEach(() => {
  mocks.fetchAPI.mockResolvedValue({})
  mocks.moreInfo.mockResolvedValue({})
  mocks.quote.mockResolvedValue(QUOTE)
  mocks.klineSummary.mockResolvedValue(null)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('HeaderBand 快慢车道(perf 回归)', () => {
  it('慢摘要在途不阻塞出壳: 行情先落位, 刷新转圈在快车道落定即停', async () => {
    const quote = defer()
    mocks.quote.mockReturnValue(quote.promise)
    mocks.klineSummary.mockReturnValue(new Promise(() => {})) // 摘要永挂起

    render(<HeaderBand symbol="002636" market="CN" type="stock" />)

    // 快车道在途 ⇒ 刷新图标在转
    expect(refreshIconClass()).toContain('animate-spin')

    // 行情回来(摘要仍挂起) ⇒ 顶行出壳, 转圈停止 —— 不被慢摘要拖住
    await act(async () => {
      quote.resolve(QUOTE)
    })
    await waitFor(() => expect(screen.getByText('平安银行')).toBeTruthy())
    await waitFor(() => expect(refreshIconClass()).not.toContain('animate-spin'))
  })

  it('摘要走慢车道: 首帧只发快车道请求, 摘要错峰后发且带显式 45s 超时', async () => {
    render(<HeaderBand symbol="002636" market="CN" type="stock" />)

    // 首帧: 摘要还没被请求(错峰到首帧之后)
    expect(mocks.klineSummary).not.toHaveBeenCalled()
    // 快车道请求已在(quote/more-info/l2)
    expect(mocks.quote).toHaveBeenCalledTimes(1)

    // 错峰窗口后发出, 且显式 45s 超时
    await waitFor(() => expect(mocks.klineSummary).toHaveBeenCalledTimes(1))
    expect(mocks.klineSummary).toHaveBeenCalledWith('002636', 'CN', { timeoutMs: 45000 })
  })

  it('摘要失败 → 显式降级(不出建议条), 不编造, 快照行照常', async () => {
    mocks.klineSummary.mockRejectedValue(new Error('summary-down'))
    render(<HeaderBand symbol="002636" market="CN" type="stock" />)

    await waitFor(() => expect(mocks.klineSummary).toHaveBeenCalled())
    // 行情已出壳
    expect(screen.getByText('平安银行')).toBeTruthy()
    // 建议条需要 summary ⇒ 摘要在途/失败时不出(不拿假建议糊弄)
    expect(screen.queryByText(/建议·/)).toBeNull()
  })

  it('非个股不发摘要(闸门不变): 慢车道只对个股开', async () => {
    render(<HeaderBand symbol="000001" market="CN" type="index" />)
    await new Promise((r) => setTimeout(r, 500)) // 超过错峰窗口
    expect(mocks.klineSummary).not.toHaveBeenCalled()
    expect(mocks.quote).not.toHaveBeenCalled()
  })
})
