// @vitest-environment jsdom
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { DrawdownChart, RealizedPnlChart } from '../../src/components/PnlCharts'

describe('PnlCharts 渲染 (B5.1/B5.2)', () => {
  it('DrawdownChart: 数据不足时给占位提示', () => {
    render(<DrawdownChart data={[{ date: '2026-09-01', equity: 100 }]} />)
    expect(screen.getByText('暂无足够数据绘制回撤')).toBeTruthy()
  })

  it('DrawdownChart: 有数据时渲染路径与刻度', () => {
    const { container } = render(
      <DrawdownChart data={[
        { date: '2026-09-01', equity: 100 },
        { date: '2026-09-02', equity: 80 },
        { date: '2026-09-03', equity: 90 },
      ]} />,
    )
    const paths = container.querySelectorAll('path')
    expect(paths.length).toBeGreaterThanOrEqual(2)   // 面积 + 折线
    expect(paths[paths.length - 1].getAttribute('d')).toContain('M')
    expect(container.textContent).toContain('-20.0%') // 最差回撤刻度
  })

  it('RealizedPnlChart: 成交不足时给占位提示', () => {
    render(<RealizedPnlChart trades={[{ exit_date: '2026-09-01', pnl: 10 }]} />)
    expect(screen.getByText('暂无足够成交绘制已实现盈亏')).toBeTruthy()
  })

  it('RealizedPnlChart: 逐点画圆点, 累计值正确', () => {
    const { container } = render(
      <RealizedPnlChart trades={[
        { exit_date: '2026-09-01', pnl: -50 },
        { exit_date: '2026-09-02', pnl: 100 },
        { exit_date: '2026-09-03', pnl: 30 },
      ]} />,
    )
    expect(container.querySelectorAll('circle')).toHaveLength(3)
    expect(container.textContent).toContain('80') // 累计终点 -50+100+30
  })
})
