// @vitest-environment jsdom
/**
 * UsageBar 回归（2026-10-22）。
 *
 * 覆盖：
 *  1. 空数据（null）→ 显示 `--`，**不出现 0**（缺数据 ≠ 用量为 0，R7 门禁口径）；
 *  2. 配额 85% → 橙色类名；
 *  3. 配额 120% → 红色类名。
 *
 * 断言风格：只用 vitest 原生 matcher，不引 jest-dom（tsc -b 会扫 src）。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { UsageBar } from '@/components/copilot/UsageBar'

afterEach(cleanup)

describe('UsageBar 空数据语义', () => {
  it('tokens / cost / calls 为 null → 全部显示 `--`，页面里不出现 0', () => {
    render(<UsageBar tokens={null} cost={null} calls={null} quota={null} used={null} />)

    expect(screen.getByTestId('copilot-usage-tokens').textContent).toBe('--')
    expect(screen.getByTestId('copilot-usage-cost').textContent).toBe('--')
    expect(screen.getByTestId('copilot-usage-calls').textContent).toBe('--')
    // 配额没给上限 → 百分比不给（不是 0%）
    expect(screen.getByTestId('copilot-usage-quota-text').textContent).toBe('--')

    // 整条 bar 的文本里不能出现孤零零的 0
    const barText = screen.getByTestId('copilot-usage-bar').textContent ?? ''
    expect(barText).not.toMatch(/0/)
  })

  it('只给部分值时，缺的仍是 `--`（不互相补齐）', () => {
    render(<UsageBar tokens={1234} cost={null} calls={7} />)
    expect(screen.getByTestId('copilot-usage-tokens').textContent).toBe('1,234')
    expect(screen.getByTestId('copilot-usage-cost').textContent).toBe('--')
    expect(screen.getByTestId('copilot-usage-calls').textContent).toBe('7')
  })

  it('有值时正常显示：tokens 千分位 / 成本带 ¥ / 次数整数', () => {
    render(<UsageBar tokens={1234567} cost={12.345} calls={42} />)
    expect(screen.getByTestId('copilot-usage-tokens').textContent).toBe('1,234,567')
    expect(screen.getByTestId('copilot-usage-cost').textContent).toBe('¥12.35')
    expect(screen.getByTestId('copilot-usage-calls').textContent).toBe('42')
  })
})

describe('UsageBar 配额进度', () => {
  it('85% → 橙色（warn）', () => {
    render(<UsageBar tokens={100} cost={1} calls={3} quota={100} used={85} />)

    const text = screen.getByTestId('copilot-usage-quota-text')
    expect(text.textContent).toBe('85%')
    expect(text.getAttribute('class')).toContain('text-amber-500')
    expect(text.getAttribute('data-level')).toBe('warn')

    const bar = screen.getByTestId('copilot-usage-quota-bar')
    expect(bar.getAttribute('class')).toContain('bg-amber-500')
    expect(bar.getAttribute('data-level')).toBe('warn')
  })

  it('120% → 红色（over）', () => {
    render(<UsageBar tokens={100} cost={1} calls={3} quota={100} used={120} />)

    const text = screen.getByTestId('copilot-usage-quota-text')
    expect(text.textContent).toBe('120%')
    expect(text.getAttribute('class')).toContain('text-red-500')
    expect(text.getAttribute('data-level')).toBe('over')

    const bar = screen.getByTestId('copilot-usage-quota-bar')
    expect(bar.getAttribute('class')).toContain('bg-red-500')
    expect(bar.getAttribute('data-level')).toBe('over')
  })

  it('50% → 中性灰（不越级变橙/红）', () => {
    render(<UsageBar tokens={100} cost={1} calls={3} quota={100} used={50} />)
    const text = screen.getByTestId('copilot-usage-quota-text')
    expect(text.textContent).toBe('50%')
    expect(text.getAttribute('class')).toContain('text-muted-foreground')
    expect(text.getAttribute('data-level')).toBe('ok')
  })

  it('有上限但没有已用量 → 百分比仍是 `--`（不是 0%），上限另起一格显示', () => {
    render(<UsageBar tokens={100} cost={1} calls={3} quota={100} used={null} />)
    expect(screen.getByTestId('copilot-usage-quota-text').textContent).toBe('--')
    expect(screen.getByTestId('copilot-usage-quota-limit').textContent).toBe('/ 100')
  })
})
