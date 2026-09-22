// @vitest-environment jsdom
/**
 * Sparkline 的 **DOM 契约**(2026-09-22)。
 *
 * 为什么单开一条: 列表行接线是"有没有"的问题 —— 而"有没有"必须看真实 DOM。
 * 生产端到端探针只能验证到"接口被调用且 200"(admin 账号没有持仓行 ⇒ 页面上无行可画),
 * 所以组件这一层用 jsdom 把契约钉死: 画出来必须是 `svg[data-sparkline]`, 且来源要能被探针读到。
 */
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import Sparkline from '@panwatch/biz-ui/components/Sparkline'

describe('Sparkline DOM 契约', () => {
  it('够点数 → 产出 svg[data-sparkline], 方向进属性', () => {
    const { container } = render(<Sparkline values={[10, 11, 10.5, 12]} />)
    const svg = container.querySelector('svg[data-sparkline]')
    expect(svg).toBeTruthy()
    expect(svg!.getAttribute('data-sparkline')).toBe('up')
  })

  it('带 source → 属性 + tooltip + aria-label 都能读到(口径可见)', () => {
    const { container } = render(<Sparkline values={[10, 11, 12]} source="tq" />)
    const svg = container.querySelector('svg[data-sparkline]')!
    expect(svg.getAttribute('data-source')).toBe('tq')
    expect(container.querySelector('title')?.textContent).toContain('tq')
    expect(svg.getAttribute('aria-label')).toContain('tq')
  })

  it('点数不足 → 不画(不编造形状)', () => {
    const { container } = render(<Sparkline values={[10, 11]} />)
    expect(container.querySelector('svg[data-sparkline]')).toBeNull()
  })

  it('含 null 的序列: 有效点不足仍然不画', () => {
    const { container } = render(<Sparkline values={[10, null, null]} />)
    expect(container.querySelector('svg[data-sparkline]')).toBeNull()
  })

  it('跌向 → data-sparkline=down', () => {
    const { container } = render(<Sparkline values={[12, 11, 10]} />)
    expect(container.querySelector('svg[data-sparkline]')!.getAttribute('data-sparkline')).toBe('down')
  })
})
