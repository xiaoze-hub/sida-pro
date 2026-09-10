// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

// A1 看板定制对话框 (2026-09-10): 11 模块 × 3 分区, 显隐开关 + 上下移 + 重置。
import DashboardCustomizer from '../../src/components/DashboardCustomizer'
import { defaultLayout, toggleModule } from '../../src/lib/dashboard-layout'

afterEach(cleanup)

const MODULE_IDS = [
  'indices', 'kpi', 'overview', 'fundflow',
  'anomalies', 'breadth',
  'agenda', 'portfolio', 'picks', 'reports', 'discover',
]

function renderDialog(overrides: Record<string, unknown> = {}) {
  return render(
    <DashboardCustomizer
      open
      onOpenChange={() => {}}
      layout={defaultLayout()}
      onToggle={() => {}}
      onMove={() => {}}
      onReset={() => {}}
      {...overrides}
    />,
  )
}

describe('DashboardCustomizer', () => {
  it('列出全部 11 个模块与 3 个分区标题', () => {
    renderDialog()
    for (const id of MODULE_IDS) {
      expect(document.querySelector(`[data-module="${id}"]`)).toBeTruthy()
    }
    expect(screen.getByText('全宽区')).toBeTruthy()
    expect(screen.getByText('双列区')).toBeTruthy()
    expect(screen.getByText('工作台与次级')).toBeTruthy()
  })

  it('开关回调 onToggle; 首项"上移"禁用; "下移"回调 onMove', () => {
    const onToggle = vi.fn()
    const onMove = vi.fn()
    renderDialog({ onToggle, onMove })
    fireEvent.click(screen.getAllByRole('switch')[0])
    expect(onToggle).toHaveBeenCalledWith('indices')
    expect((screen.getAllByTitle('上移')[0] as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getAllByTitle('下移')[0])
    expect(onMove).toHaveBeenCalledWith('indices', 1)
  })

  it('组内末项"下移"禁用(duo 组 boundary 同时成立)', () => {
    renderDialog()
    const downs = screen.getAllByTitle('下移') as HTMLButtonElement[]
    const ups = screen.getAllByTitle('上移') as HTMLButtonElement[]
    expect(downs[downs.length - 1].disabled).toBe(true) // workspace 末项 discover
    expect(ups[0].disabled).toBe(true)
  })

  it('隐藏项显示删除线、开关为关', () => {
    renderDialog({ layout: toggleModule(defaultLayout(), 'kpi') })
    const row = document.querySelector('[data-module="kpi"]') as HTMLElement
    expect(row.textContent).toContain('市场 KPI 带')
    expect(row.querySelector('.line-through')).toBeTruthy()
    expect(screen.getAllByRole('switch')[1].getAttribute('data-state')).toBe('unchecked')
  })

  it('重置按钮回调 onReset', () => {
    const onReset = vi.fn()
    renderDialog({ onReset })
    fireEvent.click(screen.getByText('重置'))
    expect(onReset).toHaveBeenCalled()
  })
})
