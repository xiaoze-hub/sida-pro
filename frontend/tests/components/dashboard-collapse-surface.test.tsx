// @vitest-environment jsdom
import { useEffect } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import CollapsibleSection from '@/components/dashboard/CollapsibleSection'

beforeEach(() => localStorage.clear())
afterEach(cleanup)

describe('看板折叠区的空间与生命周期', () => {
  it('收起时只挂标题，展开在不透明面板内挂载内容，关闭时卸载并通知布局', () => {
    const mounted = vi.fn()
    const unmounted = vi.fn()
    const onCollapsedChange = vi.fn()
    const onExpand = vi.fn()
    function Body() { useEffect(() => { mounted(); return unmounted }, []); return <div>真实内容</div> }
    const view = render(<CollapsibleSection id="example" header={<h2>测试模块</h2>} onExpand={onExpand} onCollapsedChange={onCollapsedChange}><Body /></CollapsibleSection>)
    const section = screen.getByTestId('evidence-example')
    expect(section.classList.contains('bg-card')).toBe(true)
    expect(section.classList.contains('self-start')).toBe(true)
    expect(screen.queryByTestId('evidence-body-example')).toBeNull()
    expect(mounted).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '展开' }))
    expect(screen.getByTestId('evidence-body-example').textContent).toContain('真实内容')
    expect(mounted).toHaveBeenCalledTimes(1)
    expect(onExpand).toHaveBeenCalledTimes(1)
    expect(onCollapsedChange).toHaveBeenLastCalledWith(false)
    fireEvent.click(screen.getByRole('button', { name: '收起' }))
    expect(screen.queryByTestId('evidence-body-example')).toBeNull()
    expect(unmounted).toHaveBeenCalledTimes(1)
    expect(onCollapsedChange).toHaveBeenLastCalledWith(true)
    view.unmount()
    render(<CollapsibleSection id="example" header="测试模块"><div>内容</div></CollapsibleSection>)
    expect(screen.getByRole('button', { name: '展开' }).getAttribute('aria-expanded')).toBe('false')
  })
})
