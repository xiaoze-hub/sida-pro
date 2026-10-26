// @vitest-environment jsdom
/**
 * 证据齐套度条 + 回答内价格锚定（2026-10-23）。
 *
 * 守五件事：
 *  ① 条上三态各自可见：`✓` / `⏳` / `未取到`，失败 pill 用 role-risk；
 *  ② 分母未知时只显示「已取 N 项」，**不出现 `/`**（不编总数）；
 *  ③ 空列表不渲染（空 ≠ 0/0）；
 *  ④ AnswerCard 传了 `onAnchoredPrice` 才出 chip（testid = answer-price-anchor），
 *     没传时逐字渲染原文、一个 testid 都没有（向后兼容）；
 *  ⑤ 点 chip 回传解析后的数值 + kind，百分比永远不会变成 chip。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { EvidenceReadinessStrip } from '@/components/copilot/EvidenceReadinessStrip'
import { AnswerCard } from '@/components/copilot/AnswerCard'
import { parseAnswer } from '@/lib/copilot/answer-blocks'
import type { ReadinessItem } from '@/lib/copilot/evidence-readiness'

const items: ReadinessItem[] = [
  { key: 'get_stock_quote', label: '行情', group: '走势', status: 'ok' },
  { key: 'get_delta_series', label: '逐笔', group: '主力', status: 'pending' },
  { key: 'get_cancel_order_intent', label: '撤单', group: '筹码', status: 'failed' },
]

const RAW = `【结论】洗盘吸筹偏多，站稳支撑 32.80 后再看，置信度 6/10
【依据】
- 逐笔主力净流出 552 万
- 底仓成本 40.16 元
- 今日 -1.9%
【预案】持仓者：跌破 33.20 元后重新评估
【口径】腾讯逐笔 + 自算CYQ
以上分析仅供参考，不构成投资建议。`

afterEach(cleanup)

describe('EvidenceReadinessStrip', () => {
  it('三态各一颗 pill：✓ / ⏳ / 未取到，失败用 role-risk', () => {
    render(<EvidenceReadinessStrip items={items} done={1} total={3} failed={1} />)

    expect(screen.getByTestId('evidence-readiness')).toBeTruthy()
    const pills = screen.getAllByTestId('evidence-readiness-item')
    expect(pills).toHaveLength(3)

    const ok = pills.find((p) => p.getAttribute('data-key') === 'get_stock_quote')!
    const pending = pills.find((p) => p.getAttribute('data-key') === 'get_delta_series')!
    const failed = pills.find((p) => p.getAttribute('data-key') === 'get_cancel_order_intent')!

    expect(ok.getAttribute('data-status')).toBe('ok')
    expect(ok.textContent).toContain('行情')
    expect(ok.textContent).toContain('✓')

    expect(pending.getAttribute('data-status')).toBe('pending')
    expect(pending.textContent).toContain('逐笔')
    expect(pending.textContent).toContain('⏳')

    expect(failed.getAttribute('data-status')).toBe('failed')
    expect(failed.textContent).toContain('撤单')
    expect(failed.textContent).toContain('未取到')
    expect(failed.className).toContain('role-risk')
    // 成功不用「喜庆色」：ok pill 走 muted
    expect(ok.className).not.toContain('role-risk')
  })

  it('计数：有分母显示 x/y，失败数单独标出', () => {
    render(<EvidenceReadinessStrip items={items} done={1} total={3} failed={1} />)
    const strip = screen.getByTestId('evidence-readiness')
    expect(strip.textContent).toContain('已取 1/3')
    expect(screen.getByTestId('evidence-readiness-failed-count').textContent).toContain('未取到 1')
  })

  it('分母未知时只说「已取 N 项」，绝不出现斜杠（不编总数）', () => {
    render(<EvidenceReadinessStrip items={items} done={2} total={null} />)
    const strip = screen.getByTestId('evidence-readiness')
    expect(strip.textContent).toContain('已取 2 项')
    expect(strip.textContent).not.toContain('/')
  })

  it('failed 缺省时按 items 里的 failed 统计', () => {
    render(<EvidenceReadinessStrip items={items} done={1} total={3} />)
    expect(screen.getByTestId('evidence-readiness-failed-count').textContent).toContain('未取到 1')
  })

  it('空列表不渲染（空 ≠ 0/0）', () => {
    const { container } = render(<EvidenceReadinessStrip items={[]} done={0} total={0} />)
    expect(container.firstChild).toBeNull()
    expect(screen.queryByTestId('evidence-readiness')).toBeNull()
  })
})

describe('AnswerCard 价格锚定', () => {
  it('不传 onAnchoredPrice：逐字渲染原文，不出任何 anchor testid', () => {
    const parsed = parseAnswer(RAW)
    render(<AnswerCard parsed={parsed} />)

    expect(screen.queryAllByTestId('answer-price-anchor')).toHaveLength(0)
    expect(screen.getByTestId('copilot-answer-card').textContent).toContain('支撑 32.80')
    expect(screen.getByTestId('copilot-answer-card').textContent).toContain('底仓成本 40.16 元')
  })

  it('传了 onAnchoredPrice：价位变 chip，百分比不算', () => {
    const parsed = parseAnswer(RAW)
    render(<AnswerCard parsed={parsed} onAnchoredPrice={() => {}} />)

    const chips = screen.getAllByTestId('answer-price-anchor')
    // 裁决里的 32.80 + 依据里的 40.16；-1.9% 不是价位
    expect(chips).toHaveLength(2)
    expect(chips.map((c) => c.getAttribute('data-kind'))).toEqual(['level', 'price'])
    expect(chips[0].textContent).toContain('32.80')
    expect(chips[0].textContent).toContain('支撑')
  })

  it('点 chip 回传解析后的数值 / kind / context', () => {
    const parsed = parseAnswer(RAW)
    const onAnchoredPrice = vi.fn()
    render(<AnswerCard parsed={parsed} onAnchoredPrice={onAnchoredPrice} />)

    const chips = screen.getAllByTestId('answer-price-anchor')
    fireEvent.click(chips[0])
    expect(onAnchoredPrice).toHaveBeenCalledTimes(1)
    expect(onAnchoredPrice.mock.calls[0][0]).toMatchObject({ value: 32.8, kind: 'level' })
    expect(onAnchoredPrice.mock.calls[0][0].context).toContain('支撑')

    fireEvent.click(chips[1])
    expect(onAnchoredPrice).toHaveBeenCalledTimes(2)
    expect(onAnchoredPrice.mock.calls[1][0]).toMatchObject({ value: 40.16, kind: 'price' })
  })

  it('无价位时不出 chip（也不会破坏原文）', () => {
    const parsed = parseAnswer(`【结论】观望，等量能\n【依据】\n- 主力净流出 552 万\n【口径】腾讯逐笔`)
    render(<AnswerCard parsed={parsed} onAnchoredPrice={() => {}} />)
    expect(screen.queryAllByTestId('answer-price-anchor')).toHaveLength(0)
    expect(screen.getByTestId('copilot-answer-card').textContent).toContain('主力净流出 552 万')
  })
})
