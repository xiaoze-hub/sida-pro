// @vitest-environment jsdom
// P0(2026-10-18 审计 F13): 龙虎榜「上榜原因」70/70 全 null → 整列 68 个 `--`, 还吃掉 1/3 表宽。
// 契约: 全空不渲染该列(标题注明源未提供); 默认最多 10 行, 超出折叠可展开。
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { FundamentalsPanel } from '@panwatch/biz-ui/components/insight/FundamentalsPanel'

afterEach(() => cleanup())

function dtRows(n: number, opts: { reason?: string | null; netBuy?: number | null } = {}) {
  const reason = opts.reason === undefined ? null : opts.reason
  const netBuy = opts.netBuy === undefined ? 120_000_000 : opts.netBuy
  return Array.from({ length: n }, (_, i) => ({
    trade_date: `2026-09-${String(28 - i).padStart(2, '0')}`,
    net_buy: netBuy,
    reason,
  }))
}

function renderPanel(dragonTiger: unknown[]) {
  return render(
    <FundamentalsPanel
      data={{ dragon_tiger: dragonTiger } as never}
      loading={false}
      loaded
    />,
  )
}

describe('FundamentalsPanel 龙虎榜', () => {
  it('上榜原因全为 null → 不渲染该列, 也不出现 -- 占位', () => {
    renderPanel(dtRows(68))
    const section = screen.getByTestId('dragon-tiger-section')
    const headers = within(section).getAllByRole('columnheader').map((th) => th.textContent ?? '')
    expect(headers).toEqual(['日期', '净买入'])
    expect(within(section).queryByText('上榜原因')).toBeNull()
    expect(screen.getByTestId('dragon-tiger-reason-missing')).toBeTruthy()
    expect(within(section).queryByText('--')).toBeNull()
  })

  it('默认最多 10 行, 68 行不整页铺开', () => {
    renderPanel(dtRows(68))
    const section = screen.getByTestId('dragon-tiger-section')
    expect(within(section).getAllByRole('row')).toHaveLength(1 + 10) // 表头 + 10 行
    expect(screen.getByTestId('dragon-tiger-toggle').textContent).toContain('展开全部 68 条')
  })

  it('展开后显示全部行, 再点收起回到 10 行', () => {
    renderPanel(dtRows(68))
    const section = screen.getByTestId('dragon-tiger-section')
    fireEvent.click(screen.getByTestId('dragon-tiger-toggle'))
    expect(within(screen.getByTestId('dragon-tiger-section')).getAllByRole('row')).toHaveLength(69)
    fireEvent.click(screen.getByTestId('dragon-tiger-toggle'))
    expect(within(section).getAllByRole('row')).toHaveLength(1 + 10)
  })

  it('有原因时照常渲染该列并用真实值', () => {
    renderPanel(dtRows(3, { reason: '日涨幅偏离值达 7%' }))
    const section = screen.getByTestId('dragon-tiger-section')
    expect(within(section).getByText('上榜原因')).toBeTruthy()
    expect(within(section).getAllByText('日涨幅偏离值达 7%')).toHaveLength(3)
    expect(screen.queryByTestId('dragon-tiger-reason-missing')).toBeNull()
  })

  it('净买入全空 → 该列也同样隐藏(不留恒空列)', () => {
    renderPanel(dtRows(5, { netBuy: null, reason: '换手率异常' }))
    const headers = within(screen.getByTestId('dragon-tiger-section'))
      .getAllByRole('columnheader')
      .map((th) => th.textContent ?? '')
    expect(headers).toEqual(['日期', '上榜原因'])
  })
})
