// @vitest-environment jsdom
// P0(2026-10-18 审计 F19/R19): 问财表代码列曾整列显示 thsdk 前缀码 `USZA000006`,
// 破坏复制/跳转/搜索。展示层必须归一成 6 位。
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@sida/api', () => ({
  fetchAPI: (...a: unknown[]) => mocks.fetchAPI(...a),
}))

import WencaiPanel from '@sida/biz-ui/components/WencaiPanel'

afterEach(() => {
  cleanup()
  mocks.fetchAPI.mockReset()
})

describe('WencaiPanel 代码列', () => {
  it('剥掉 USZA/USHT 内部前缀, 只显示 6 位代码', async () => {
    mocks.fetchAPI.mockResolvedValue({
      available: true,
      note: '',
      rows: [
        { symbol: 'USZA000006', name: '深振业A', 涨幅: 3.2 },
        { symbol: 'USHT603922', name: '长鑫科技', 涨幅: 1.1 },
      ],
    })
    render(<WencaiPanel />)
    expect(await screen.findByText('000006')).toBeTruthy()
    expect(screen.getByText('603922')).toBeTruthy()
    // 原始前缀码不得出现在任何可见文本里
    expect(screen.queryByText(/USZA000006/)).toBeNull()
    expect(screen.queryByText(/USHT603922/)).toBeNull()
    expect(document.body.textContent).not.toMatch(/US[A-Z]{2}\d{6}/)
  })
})
