// @vitest-environment jsdom
/**
 * 晋级漏斗（PromotionFunnel）契约测试 — 2026-10-24 补图
 *
 * 锁死四件事：
 *  ① 每档一行，柱宽随家数递减（断崖可视化），且首板最长；
 *  ② `count === 0` 显示「无」与 `--`，**不画 0 宽空条**（0 家 ≠ 没采集到）；
 *  ③ 占比是「该档家数 / 当日涨停家数」，缺 totalLimitUp 时不显示占比列；
 *  ④ **不出现分层晋级率**——表内未存（per_level_available=false），本图不许现算。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import PromotionFunnel from '../PromotionFunnel'

const lv = (board: number, count: number) => ({
  board,
  count,
  members: [],
  members_truncated: false,
})

afterEach(cleanup)

describe('PromotionFunnel 家数断崖', () => {
  it('每档一行，且首板柱宽最长（断崖可见）', () => {
    render(<PromotionFunnel levels={[lv(1, 70), lv(2, 9), lv(3, 1), lv(6, 2)]} totalLimitUp={82} />)
    expect(screen.getByTestId('promotion-funnel-row-1')).toBeTruthy()
    expect(screen.getByTestId('promotion-funnel-row-6')).toBeTruthy()
    const w1 = screen.getByTestId('promotion-funnel-bar-1').style.width
    const w2 = screen.getByTestId('promotion-funnel-bar-2').style.width
    expect(parseFloat(w1)).toBeGreaterThan(parseFloat(w2))
    expect(parseFloat(w1)).toBe(100)
  })

  it('家数为 0 的档显示「无」与 --，不画空条', () => {
    render(<PromotionFunnel levels={[lv(1, 5), lv(4, 0)]} />)
    expect(screen.queryByTestId('promotion-funnel-bar-4')).toBeNull()
    expect(screen.getByTestId('promotion-funnel-row-4').textContent).toContain('无')
    expect(screen.getByTestId('promotion-funnel-row-4').textContent).toContain('--')
  })

  it('占比 = 该档 / 当日涨停；缺 totalLimitUp 时不显示占比', () => {
    const { unmount } = render(<PromotionFunnel levels={[lv(1, 41), lv(2, 9)]} totalLimitUp={82} />)
    expect(screen.getByTestId('promotion-funnel-row-1').textContent).toContain('占涨停 50%')
    unmount()
    render(<PromotionFunnel levels={[lv(1, 41)]} />)
    expect(screen.getByTestId('promotion-funnel-row-1').textContent).not.toContain('占涨停')
  })

  it('显式声明不含分层晋级率（表内未存），且不出现百分号形式的晋级率', () => {
    render(<PromotionFunnel levels={[lv(1, 70), lv(2, 9)]} totalLimitUp={82} />)
    const root = screen.getByTestId('promotion-funnel')
    expect(root.textContent).toContain('不含分层晋级率（表内未存）')
    expect(root.textContent).not.toMatch(/晋级率\s*\d/)
  })

  it('空梯队给明确文案而不是空白', () => {
    render(<PromotionFunnel levels={[]} />)
    expect(screen.getByText(/当日无梯队家数/)).toBeTruthy()
  })
})
