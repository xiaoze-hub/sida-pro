// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

// A6 面板联动开关 (2026-09-10): 默认跟随当前标的, 可一键锁定面板自己的标的(对比场景)。
import PanelLockToggle from '../../src/components/PanelLockToggle'

afterEach(cleanup)

describe('PanelLockToggle (A6)', () => {
  it('未锁定: 显示"跟随 当前标的", 点击触发 onToggle(去锁定)', () => {
    const onToggle = vi.fn()
    render(<PanelLockToggle symbol="600519" locked={null} onToggle={onToggle} />)
    expect(screen.getByText(/跟随/)).toBeTruthy()
    expect(screen.getByText(/600519/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button'))
    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it('已锁定: 显示锁定标的, 点击触发 onToggle(解锁)', () => {
    const onToggle = vi.fn()
    render(<PanelLockToggle symbol="000001" locked="600519" onToggle={onToggle} />)
    expect(screen.getByText(/已锁定/)).toBeTruthy()
    expect(screen.getByText(/600519/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button'))
    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it('锁定标的与当前浏览不同: 显式提示当前浏览标的(对比态)', () => {
    render(<PanelLockToggle symbol="000001" locked="600519" onToggle={() => {}} />)
    expect(screen.getByText(/当前浏览/)).toBeTruthy()
  })

  it('锁定标的等于当前浏览: 不显示对比提示', () => {
    render(<PanelLockToggle symbol="600519" locked="600519" onToggle={() => {}} />)
    expect(screen.queryByText(/当前浏览/)).toBeNull()
  })
})
