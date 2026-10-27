// @vitest-environment jsdom
/**
 * 四档资金结构条带图契约（2026-10-24）。
 *
 * 锁三件事，都是「错了会被读成另一种意思」的地方：
 *  ① 方向色：净额 >0 必须红（A 股买红卖绿），不能反；
 *  ② 空态诚实：无数据要说明「为什么没有」，不能渲染成一个空框让人以为没跌没涨；
 *  ③ 口径可见：阈值必须标「推断值」—— TQ 不返回档位阈值，写成精确分档就是编数据。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import Fund4Strip, { type Fund4Day } from '@sida/biz-ui/components/Fund4Strip'

afterEach(cleanup)

const day = (d: string, xl: number, lg: number): Fund4Day => ({
  trade_date: d,
  active_ratio: 55.5,
  tiers: {
    xl: { buy: xl, sell: 0, net: xl },
    lg: { buy: lg, sell: 0, net: lg },
    md: { buy: 0, sell: 0, net: 0 },
    sm: { buy: 0, sell: 0, net: 0 },
  },
})

describe('Fund4Strip', () => {
  it('净额为正的柱标 up、为负的柱标 down（买红卖绿由 --stock-up/down 承载）', () => {
    const { container } = render(<Fund4Strip days={[day('20261023', 1e8, -5e7)]} />)
    const ups = container.querySelectorAll('rect[data-dir="up"]')
    const downs = container.querySelectorAll('rect[data-dir="down"]')
    expect(ups.length).toBe(1) // 特大单净流入
    expect(downs.length).toBe(1) // 大单净流出
    expect((ups[0] as SVGRectElement).style.fill).toContain('--stock-up')
    expect((downs[0] as SVGRectElement).style.fill).toContain('--stock-down')
  })

  it('口径徽章在，且阈值标注为推断值', () => {
    render(<Fund4Strip days={[day('20261023', 1e8, 1e7)]} />)
    expect(screen.getByText('四档资金结构')).toBeTruthy()
    expect(document.querySelector('[data-caliber="tdx_exday4"]')).toBeTruthy()
    expect(screen.getByText(/推断值/)).toBeTruthy()
  })

  it('无数据时给出原因，不渲染空图', () => {
    render(<Fund4Strip days={[]} hint="exday_daily 无该标的记录" />)
    expect(screen.getByText('exday_daily 无该标的记录')).toBeTruthy()
    expect(document.querySelectorAll('rect').length).toBe(0)
  })

  it('加载中与「无数据」是两个态，不能混', () => {
    render(<Fund4Strip days={[]} loading />)
    expect(screen.getByText(/加载中/)).toBeTruthy()
    expect(screen.queryByText(/暂无四档历史/)).toBeNull()
  })
})
