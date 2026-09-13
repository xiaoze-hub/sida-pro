// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 带1 `HeaderBand` 的**刷新**语义(Task 7 复审 Finding 1, 控制器裁定「页面级刷新」)。
 *
 * 守两件事:
 *  ① 点刷新按钮 = 本带行情重取(`tick` → 重新发 `GET /quotes/{s}`)**且**回调 `onRefresh`
 *     —— 页面(`StockWorkbench`)靠它把正文子树(指数/板块正文、个股带2)重挂载 ⇒ 重新取数;
 *  ② `onRefresh` 可选: 不传时行为与旧版一致(只刷自身, 不抛)。
 *
 * 说明: 页面测试(`stock-workbench.test.tsx`)把本组件 mock 掉了, 故"真组件是否真的调了
 * `onRefresh`"必须由本文件守 —— 否则把回调漏调, 页面测试仍会全绿。
 * 真数据纪律: 本文件 mock 的是**网络层**(`@panwatch/api`), 组件取数/渲染走真实代码。
 * 用 `type="index"` 以最小化数据面(仅 `/quotes`, 不发 CN-only 的 more-info / l2 / klineSummary)。
 */

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

const QUOTE = { name: '上证指数', current_price: 3200.5, change_pct: 0.42, open_price: 3190 }

beforeEach(() => {
  mocks.fetchAPI.mockResolvedValue({})
  mocks.quote.mockResolvedValue(QUOTE)
  mocks.moreInfo.mockResolvedValue({})
  mocks.klineSummary.mockResolvedValue(null)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const refreshButton = () => screen.getByRole('button', { name: '刷新' })

describe('HeaderBand 刷新(页面级)', () => {
  it('点刷新: 自身行情重取 + 广播 onRefresh(正文据此重挂载重取数)', async () => {
    const onRefresh = vi.fn()
    render(<HeaderBand symbol="000001" market="CN" type="index" onRefresh={onRefresh} />)

    // 挂载即取数
    await waitFor(() => expect(mocks.quote).toHaveBeenCalledTimes(1))
    expect(mocks.quote).toHaveBeenLastCalledWith('000001', 'CN')
    expect(onRefresh).not.toHaveBeenCalled()

    fireEvent.click(refreshButton())

    // ① 自身行情重取
    await waitFor(() => expect(mocks.quote).toHaveBeenCalledTimes(2))
    // ② 页面级广播**恰好一次**(每次点击一次; 不多不少)
    expect(onRefresh).toHaveBeenCalledTimes(1)

    fireEvent.click(refreshButton())
    await waitFor(() => expect(mocks.quote).toHaveBeenCalledTimes(3))
    expect(onRefresh).toHaveBeenCalledTimes(2)
  })

  it('不传 onRefresh: 仍只刷自身行情(向后兼容, 不抛)', async () => {
    render(<HeaderBand symbol="000001" market="CN" type="index" />)
    await waitFor(() => expect(mocks.quote).toHaveBeenCalledTimes(1))

    fireEvent.click(refreshButton())
    await waitFor(() => expect(mocks.quote).toHaveBeenCalledTimes(2))
    // index 类型不发 CN-only 数据面(more-info / l2 / klineSummary)
    expect(mocks.moreInfo).not.toHaveBeenCalled()
    expect(mocks.fetchAPI).not.toHaveBeenCalled()
    expect(mocks.klineSummary).not.toHaveBeenCalled()
  })
})
