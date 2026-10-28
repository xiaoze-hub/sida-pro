// @vitest-environment jsdom
/**
 * P0 自评语义：回答里的「置信度 x/10」是**模型自评**，不是统计置信度/准确率。
 *
 * 缺陷链（修复前）：
 *   answer-blocks.extractConfidence → 8（原始 x/10）
 *   → AnswerCard `Math.round(confidence * 10)` → 80（被放大成百分制）
 *   → DecisionCompassCard `{confidence}%` → 用户看到「置信度 80%」
 * 结果：模型自评被包装成统计百分比，凭空造出一个"80% 把握"的量化结论。
 *
 * 修复口径：
 *   - 只显示原始 x/10，并显式标注「模型自评」；
 *   - 缺失 / 非有限 / 越界（不在 0~10）→ 隐藏，绝不兜底编造；
 *   - 支撑、阻力、止损、目标的语义与标注一律不动。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { AnswerCard } from '@/components/copilot/AnswerCard'
import { DecisionCompassCard } from '@/components/copilot/DecisionCompassCard'
import { parseAnswer } from '@/lib/copilot/answer-blocks'

beforeEach(() => localStorage.setItem('token', `e30.${btoa(JSON.stringify({ sub: 'test-user' }))}.signature`))
afterEach(() => {
  localStorage.removeItem('token')
  cleanup()
  sessionStorage.clear()
})

const RAW_WITH_SUPPORT_RESISTANCE = `【结论】突破箱体震荡向上，置信度 8/10
【依据】
- 关键支撑位 32.80 元有大买单防守
- 上方阻力位 36.50 元附近面临套牢盘压力
- 技术面走势形成底背离金叉
【预案】回踩 32.80 不破可加仓，突破 36.50 减仓
【口径】通达信实时 + L2
以上分析不构成投资建议。`

describe('模型自评口径：回答 → 罗盘 全链', () => {
  it('8/10 必须以「模型自评 8/10」显示，不得渲染成 80% 统计置信度', () => {
    const parsed = parseAnswer(RAW_WITH_SUPPORT_RESISTANCE)
    expect(parsed.confidence).toBe(8)

    render(<AnswerCard parsed={parsed} symbol="600111" />)

    const compass = screen.getByTestId('copilot-decision-compass')
    const badge = screen.getByTestId('copilot-compass-model-confidence')

    // 保留原始 x/10 并标明来源是模型自评
    expect(badge.textContent).toMatch(/模型自评/)
    expect(badge.textContent).toMatch(/8\s*\/\s*10/)

    // 反例：被放大的伪统计百分比（修复前真实渲染为 80%）
    expect(compass.textContent).not.toMatch(/80\s*%/)
    expect(compass.textContent).not.toMatch(/置信度\s*80/)
    // 罗盘不得自称置信度/准确率（那是统计口径，没有样本支撑）
    expect(compass.textContent).not.toMatch(/准确率/)
    expect(compass.textContent).not.toMatch(/统计置信/)
  })

  it('回答里没有置信度时，罗盘不出现任何自评徽标，也不补默认值', () => {
    const raw = RAW_WITH_SUPPORT_RESISTANCE.replace('，置信度 8/10', '')
    const parsed = parseAnswer(raw)
    expect(parsed.confidence).toBeNull()

    render(<AnswerCard parsed={parsed} symbol="600111" />)

    expect(screen.getByTestId('copilot-decision-compass')).toBeTruthy()
    expect(screen.queryByTestId('copilot-compass-model-confidence')).toBeNull()
    expect(screen.getByTestId('copilot-decision-compass').textContent).not.toMatch(/\//)
  })
})

describe('DecisionCompassCard 自评入参容错', () => {
  const base = { ticker: '600111', supportPrice: 32.8, resistancePrice: 36.5 }

  it('正常 0~10 自评分原样显示，小数位保留、不补零、不取整', () => {
    render(<DecisionCompassCard {...base} modelConfidence={8.5} />)
    expect(screen.getByTestId('copilot-compass-model-confidence').textContent).toMatch(/8\.5\s*\/\s*10/)

    cleanup()
    render(<DecisionCompassCard {...base} modelConfidence={3} />)
    expect(screen.getByTestId('copilot-compass-model-confidence').textContent).toMatch(/3\s*\/\s*10/)
    expect(screen.getByTestId('copilot-compass-model-confidence').textContent).not.toMatch(/3\.0/)

    cleanup()
    render(<DecisionCompassCard {...base} modelConfidence={0} />)
    expect(screen.getByTestId('copilot-compass-model-confidence').textContent).toMatch(/0\s*\/\s*10/)
  })

  it('越界/非有限/非法值一律隐藏，绝不换算成百分比', () => {
    const bad = [80, 100, 10.5, -1, Number.NaN, Number.POSITIVE_INFINITY]
    for (const v of bad) {
      cleanup()
      render(<DecisionCompassCard {...base} modelConfidence={v} />)
      expect(screen.queryByTestId('copilot-compass-model-confidence'), `值 ${v} 应被隐藏`).toBeNull()
      const text = screen.getByTestId('copilot-decision-compass').textContent || ''
      expect(text, `值 ${v} 不得渲染成百分比`).not.toMatch(/%/)
      expect(text).not.toMatch(/模型自评/)
    }
  })

  it('缺省时不显示自评徽标', () => {
    render(<DecisionCompassCard {...base} />)
    expect(screen.queryByTestId('copilot-compass-model-confidence')).toBeNull()
  })
})

describe('自评修复不得改动支撑/阻力/止损/目标语义', () => {
  it('止损与目标只按自身名称标注，绝不改标为支撑/阻力，告警仍锚定支撑位', () => {
    const onSetAlert = vi.fn()
    render(
      <DecisionCompassCard
        ticker="600111"
        currentPrice={34}
        costPrice={35}
        supportPrice={32}
        resistancePrice={38}
        stopLossPrice={30}
        targetPrice={42}
        modelConfidence={8}
        onSetAlert={onSetAlert}
      />,
    )

    const card = screen.getByTestId('copilot-decision-compass')
    const text = card.textContent || ''

    // 四个语义各归各位
    expect(text).toMatch(/支撑 32\.00/)
    expect(text).toMatch(/阻力 38\.00/)
    expect(text).toMatch(/止损 30\.00/)
    expect(text).toMatch(/目标 42\.00/)
    // 止损/目标不是支撑/阻力：不得出现"止损位 32"这类错标
    expect(text).not.toMatch(/支撑 30\.00/)
    expect(text).not.toMatch(/阻力 42\.00/)
    expect(text).not.toMatch(/止损 32\.00/)

    // 破位告警锚定支撑位，不受自评徽标影响
    fireEvent.click(screen.getByText(/支撑破位告警/))
    expect(onSetAlert).toHaveBeenCalledTimes(1)
    expect(onSetAlert).toHaveBeenCalledWith(32)

    // 自评不是百分比：卡片里唯一的百分比只能是持仓盈亏这类真实算出来的量
    expect(text).toMatch(/持仓盈亏: -2\.86%/)
    expect(text).not.toMatch(/80\s*%/)
  })
})
