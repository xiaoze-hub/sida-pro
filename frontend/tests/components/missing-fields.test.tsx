// @vitest-environment jsdom
/**
 * P0-3 钉子: 缺数折叠组件 + 资金流水"整列缺失不摆 `--`"。
 *
 * 设计稿 v3.0 §七 6.1: 诚实口径要求缺数显式标 `--`, 但一屏 73 个 `--` 是噪声 →
 * **缺数据不该占位, 也不该被藏起来**: 聚合成一行 + 说清原因, 需要时展开看逐项。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

afterEach(cleanup)

import MissingFields, { reasonSummary, type MissingField } from '@/components/MissingFields'

const root = resolve(__dirname, '../..')
const read = (p: string) => readFileSync(resolve(root, p), 'utf-8')

describe('MissingFields 组件', () => {
  const items: MissingField[] = [
    { label: '十档买卖额', reason: 'TQ 未连接' },
    { label: '主力净额', reason: 'TQ 未连接' },
    { label: '封单成色', reason: '当日无成交' },
  ]

  it('默认收起: 只显示一行"缺 N 项 + 原因摘要", 不摆 N 个 --', () => {
    render(<MissingFields title="盘口速览" items={items} />)
    const line = screen.getByTestId('missing-fields-toggle')
    expect(line.textContent).toContain('盘口速览 本页缺 3 项')
    expect(line.textContent).toContain('TQ 未连接')       // 原因必须可见, 不许只说"缺"
    expect(screen.queryByTestId('missing-fields-list')).toBeNull()
    // 页面上没有 -- 字符(这就是本组件存在的理由)
    expect(screen.getByTestId('missing-fields').textContent).not.toContain('--')
  })

  it('展开后逐项列 label + reason(折叠的是重复, 不是真相)', () => {
    render(<MissingFields title="盘口速览" items={items} />)
    fireEvent.click(screen.getByTestId('missing-fields-toggle'))
    const list = screen.getByTestId('missing-fields-list')
    expect(list.textContent).toContain('十档买卖额')
    expect(list.textContent).toContain('封单成色')
    expect(list.textContent).toContain('当日无成交')
  })

  it('单项缺失显示"X 缺失", 不显示"本页缺 1 项"', () => {
    render(<MissingFields items={[{ label: '龙虎榜', reason: '非交易日' }]} />)
    expect(screen.getByTestId('missing-fields-toggle').textContent).toContain('龙虎榜 缺失')
  })

  it('items 为空 → 什么都不渲染(不摆空壳)', () => {
    const { container } = render(<MissingFields items={[]} />)
    expect(container.firstChild).toBeNull()
  })

  it('原因压缩: 去重, 超过 2 类折成"等 N 类原因"', () => {
    expect(reasonSummary([{ label: 'a', reason: 'r1' }, { label: 'b', reason: 'r1' }])).toBe('r1')
    expect(
      reasonSummary([
        { label: 'a', reason: 'r1' },
        { label: 'b', reason: 'r2' },
        { label: 'c', reason: 'r3' },
      ]),
    ).toBe('r1 · r2 等 3 类原因')
    expect(reasonSummary([{ label: 'a', reason: '' }])).toBe('')
  })
})

describe('资金流水表: 整列缺失不摆一列 --', () => {
  const src = read('src/pages/workbench/tabs/L2Tab.tsx')

  it('按列存在性渲染: 缺的列不渲染表头与单元格', () => {
    expect(src).toMatch(/\{hasDate && <th[^>]*>日期<\/th>\}/)
    expect(src).toMatch(/\{hasMing && <th[^>]*>明盘净额<\/th>\}/)
    expect(src).toMatch(/\{hasDark && <th[^>]*>暗盘净额<\/th>\}/)
    expect(src).toMatch(/\{hasDate && <td[^>]*>\{r\.date\}<\/td>\}/)
  })

  it('缺的列必须**说清原因**(折叠不是隐藏)', () => {
    expect(src).toContain('本源未输出日期(行序即最近 N 日)')
    expect(src).toContain('后端未输出该日净额')
    expect(src).toContain('暗盘源不可用/未输出')
    expect(src).toMatch(/<MissingFields title="资金流水"/)
  })

  it('三列全缺 → 只留说明行, 不摆空表', () => {
    expect(src).toMatch(/hasDate \|\| hasMing \|\| hasDark \?/)
    expect(src).toMatch(/最近 N 日三列均无数据/)
  })

  it('不补 0: 缺数仍走 toAmount(--) 的既有通道, 没有把 null 变成 0', () => {
    expect(src).not.toMatch(/ming_net \?\? 0/)
    expect(src).not.toMatch(/dark_net \?\? 0/)
  })
})
