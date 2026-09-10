// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

// A4 三地市场状态 (2026-09-10): 开闭市徽标(status_text) + 交易时段 + 当地时间的紧凑展示。
import MarketStatusPills from '../../src/components/MarketStatusPills'
import type { DashboardMarketStatus } from '@panwatch/api'

const ITEMS: DashboardMarketStatus[] = [
  { code: 'CN', name: 'A股', status: 'after_hours', status_text: '已收盘', is_trading: false, sessions: ['09:30-11:30', '13:00-15:00'], local_time: '21:19' },
  { code: 'HK', name: '港股', status: 'trading', status_text: '交易中', is_trading: true, sessions: ['09:30-12:00', '13:00-16:00'], local_time: '21:19' },
  { code: 'US', name: '美股', status: 'pre_market', status_text: '盘前', is_trading: false, sessions: ['09:30-16:00'], local_time: '09:19' },
]

afterEach(cleanup)

describe('MarketStatusPills (A4)', () => {
  it('每个市场一徽标: 名称 + 开闭市文案(status_text)', () => {
    render(<MarketStatusPills items={ITEMS} />)
    expect(screen.getByText('A股')).toBeTruthy()
    expect(screen.getByText('已收盘')).toBeTruthy()
    expect(screen.getByText('交易中')).toBeTruthy()
    expect(screen.getByText('盘前')).toBeTruthy()
  })

  it('交易中高亮(琥珀点), 非交易中灰点', () => {
    const { container } = render(<MarketStatusPills items={ITEMS} />)
    const hk = container.querySelector('[data-market="HK"]')!
    const cn = container.querySelector('[data-market="CN"]')!
    expect(hk.querySelector('.bg-amber-500')).toBeTruthy()
    expect(cn.querySelector('.bg-amber-500')).toBeNull()
    expect(cn.querySelector('.bg-muted-foreground\\/40')).toBeTruthy()
  })

  it('悬停 title 含交易时段与当地时间; 桌面档直显时段文案', () => {
    const { container } = render(<MarketStatusPills items={ITEMS} />)
    const cn = container.querySelector('[data-market="CN"]')!
    const title = cn.getAttribute('title') || ''
    expect(title).toContain('09:30-11:30')
    expect(title).toContain('13:00-15:00')
    expect(title).toContain('当地 21:19')
    // 桌面档(lg:inline)时段直显: DOM 里存在时段文本
    expect(cn.textContent).toContain('09:30-11:30/13:00-15:00')
  })

  it('空列表不渲染(无状态可报≠故障)', () => {
    const { container } = render(<MarketStatusPills items={[]} />)
    expect(container.textContent).toBe('')
  })
})
