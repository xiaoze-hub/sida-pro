// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { AnswerCard } from '@/components/copilot/AnswerCard'
import { parseAnswer } from '@/lib/copilot/answer-blocks'
import { ChartOverlayService } from '@/lib/copilot/chartOverlayService'

beforeEach(() => localStorage.setItem('token', `e30.${btoa(JSON.stringify({ sub: 'test-user' }))}.signature`))

afterEach(() => {
  localStorage.removeItem('token')
  cleanup()
  sessionStorage.clear()
})

describe('review-safety 价位口径安全测试 (防止止损/目标被误标为支撑/阻力)', () => {
  it('回答只包含止损位和目标价时，不得推断或改标为支撑/阻力，不得渲染支撑阻力罗盘', () => {
    const RAW = `【结论】短线博弈，置信度 7/10
【依据】
- 止损位 30.00 元
- 目标价 40.00 元
【预案】严格执行风控
【口径】行情数据
以上分析不构成投资建议。`

    const parsed = parseAnswer(RAW)
    const onAnchoredPrice = vi.fn()

    render(
      <AnswerCard
        parsed={parsed}
        symbol="600111"
        onAnchoredPrice={onAnchoredPrice}
      />,
    )

    // 严守口径：缺项不推断，绝不强行渲染支撑阻力罗盘
    expect(screen.queryByTestId('copilot-decision-compass')).toBeNull()

    // 绝不把止损 30.00 偷换为防守或支撑
    expect(screen.queryByText(/防守 30\.00/)).toBeNull()
    expect(screen.queryByText(/支撑 30\.00/)).toBeNull()

    // 绝不把目标价 40.00 偷换为阻力
    expect(screen.queryByText(/阻力 40\.00/)).toBeNull()

    // 绝不提供把止损/目标误当作支撑/阻力叠加到 K 线的按钮
    expect(screen.queryByText('在 K 线叠加画线')).toBeNull()

    // 原文行内价位锚点保留其纯正的“止损”与“目标”语义
    const anchors = screen.getAllByTestId('answer-price-anchor')
    expect(anchors).toHaveLength(2)

    expect(anchors[0].textContent).toContain('30.00')
    expect(anchors[0].textContent).toContain('止损')

    expect(anchors[1].textContent).toContain('40.00')
    expect(anchors[1].textContent).toContain('目标')
  })

  it('回答包含明确标注的支撑位与阻力位时，如实展示支撑与阻力，并支持正确画线', () => {
    const RAW = `【结论】回踩不破偏多，置信度 8/10
【依据】
- 支撑位 32.80 元具备强承接
- 阻力位 36.50 元存在套牢抛压
【预案】回踩 32.80 企稳跟进
【口径】L2盘口
以上分析不构成投资建议。`

    const parsed = parseAnswer(RAW)
    const onAnchoredPrice = vi.fn()
    const saveSpy = vi.spyOn(ChartOverlayService, 'saveOverlay')

    render(
      <AnswerCard
        parsed={parsed}
        symbol="600111"
        onAnchoredPrice={onAnchoredPrice}
      />,
    )

    // 正确渲染罗盘
    expect(screen.getByTestId('copilot-decision-compass')).toBeTruthy()
    // 文本标明确为“支撑”与“阻力”，杜绝模糊词“防守”
    expect(screen.getByText(/支撑 32\.80/)).toBeTruthy()
    expect(screen.getByText(/阻力 36\.50/)).toBeTruthy()
    expect(screen.queryByText(/防守 32\.80/)).toBeNull()

    const drawBtn = screen.getByText('在 K 线叠加画线')
    fireEvent.click(drawBtn)

    expect(saveSpy).toHaveBeenCalledWith(
      '600111',
      expect.arrayContaining([
        expect.objectContaining({ price: 32.8, kind: 'support', title: '支撑 32.8' }),
        expect.objectContaining({ price: 36.5, kind: 'pressure', title: '阻力 36.5' }),
      ]),
    )
    expect(onAnchoredPrice).toHaveBeenCalledWith(
      expect.objectContaining({
        value: 32.8,
        kind: 'level',
        context: '支撑',
      }),
    )

    saveSpy.mockRestore()
  })

  it('同时存在支撑、阻力与止损、目标时，各项严格保持自身独立语义不相互串扰', () => {
    const RAW = `【结论】区间震荡，置信度 8/10
【依据】
- 支撑位 32.80 元
- 阻力位 36.50 元
- 止损位 31.00 元
- 目标价 39.00 元
【预案】区间高抛低吸
【口径】行情数据
以上分析不构成投资建议。`

    const parsed = parseAnswer(RAW)
    render(<AnswerCard parsed={parsed} symbol="600111" />)

    expect(screen.getByTestId('copilot-decision-compass')).toBeTruthy()
    expect(screen.getByText(/支撑 32\.80/)).toBeTruthy()
    expect(screen.getByText(/阻力 36\.50/)).toBeTruthy()
    expect(screen.getByText(/止损 31\.00/)).toBeTruthy()
    expect(screen.getByText(/目标 39\.00/)).toBeTruthy()
  })
})
