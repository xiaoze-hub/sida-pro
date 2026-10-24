// @vitest-environment jsdom
/**
 * L2-F 共享组件钉子（2026-10-24）
 *
 * `Term`（G11 术语解释）：唯一出口，键盘可达，Esc 关闭，不许各页自造第二套。
 * `EmptyState`（G13 空态三件套）：已存在组件，这里补回归钉子 —— 无 action 时不许渲染空按钮容器。
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { Term } from '../../src/components/Term'
import { EmptyState } from '../../src/components/EmptyState'
import { Button } from '@panwatch/base-ui/components/ui/button'

afterEach(cleanup)

describe('Term：术语解释', () => {
  it('渲染原词与 ? 触发按钮', () => {
    render(<Term def="封板率＝收盘封死涨停的家数 ÷ 当日涨停家数">封板率</Term>)
    expect(screen.getByText('封板率')).toBeTruthy()
    expect(screen.getByRole('button', { name: '术语说明' })).toBeTruthy()
  })

  it('点击后展示解释，aria-expanded 同步', () => {
    render(<Term def="样本数不足 30 时不给命中率">晋级率</Term>)
    const btn = screen.getByRole('button', { name: '术语说明' })
    expect(btn.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(btn)
    expect(btn.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByRole('tooltip').textContent).toContain('样本数不足 30')
  })

  it('Esc 关闭气泡', () => {
    render(<Term def="按单笔成交金额分四档">四档资金</Term>)
    fireEvent.click(screen.getByRole('button', { name: '术语说明' }))
    expect(screen.queryByRole('tooltip')).toBeTruthy()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('hover 即出解释（不强迫点击）', () => {
    render(<Term def="在历史中的排位，越高越极端">p72</Term>)
    fireEvent.mouseEnter(screen.getByText('p72'))
    expect(screen.getByRole('tooltip').textContent).toContain('历史中的排位')
  })

  it('给了 href 才渲染口径文档链接', () => {
    const { rerender } = render(<Term def="逐笔主动买卖">主力意图</Term>)
    fireEvent.click(screen.getByRole('button', { name: '术语说明' }))
    expect(screen.queryByText('查看口径文档')).toBeNull()
    rerender(<Term def="逐笔主动买卖" href="/docs/caliber">主力意图</Term>)
    expect(screen.getByText('查看口径文档').getAttribute('href')).toBe('/docs/caliber')
  })
})

describe('EmptyState：空态三件套', () => {
  it('原因 + 何时有数据 + 动作三件齐全', () => {
    render(
      <EmptyState
        title="今日休市，没有竞价数据"
        description="竞价页每天 09:15 自动开始"
        action={<Button size="sm">看最近竞价日 2026-10-23</Button>}
        caliber="口径：TQ 盘口真实竞价金额"
      />,
    )
    expect(screen.getByText('今日休市，没有竞价数据')).toBeTruthy()
    expect(screen.getByText('竞价页每天 09:15 自动开始')).toBeTruthy()
    expect(screen.getByRole('button', { name: '看最近竞价日 2026-10-23' })).toBeTruthy()
    expect(screen.getByText('口径：TQ 盘口真实竞价金额')).toBeTruthy()
  })

  it('无 action 时不渲染按钮容器', () => {
    render(<EmptyState title="暂无命中记录" description="触发一次价格提醒后会出现在这里" />)
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('error 变体用 role=alert，loading 用 role=status', () => {
    const { rerender } = render(<EmptyState title="加载失败" variant="error" />)
    expect(screen.getByRole('alert')).toBeTruthy()
    rerender(<EmptyState title="加载中" variant="loading" />)
    expect(screen.getByRole('status')).toBeTruthy()
  })

  it('data-empty-state 供布局体检断言（禁止半屏留白）', () => {
    const { container } = render(<EmptyState title="暂无数据" compact />)
    const root = container.querySelector('[data-empty-state]') as HTMLElement
    expect(root).toBeTruthy()
    expect(root.className).toContain('min-h-[96px]')
  })
})
