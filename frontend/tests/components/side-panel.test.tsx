// @vitest-environment jsdom
import { useState } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { SidePanel } from '@/components/SidePanel'
function Example() { const [open, setOpen] = useState(false); return <><button onClick={() => setOpen(true)}>查看运行</button><SidePanel open={open} onOpenChange={setOpen} title="最近运行" description="查看状态和错误"><p>执行证据</p></SidePanel></> }
afterEach(cleanup)
describe('上下文侧边面板', () => {
  it('仅主动打开后出现，靠右展示，Escape 关闭并恢复入口焦点', async () => {
    render(<Example />)
    expect(screen.queryByRole('dialog')).toBeNull()
    const trigger = screen.getByRole('button', { name: '查看运行' })
    trigger.focus()
    fireEvent.click(trigger)
    const panel = screen.getByRole('dialog', { name: '最近运行' })
    expect(panel.classList.contains('right-0')).toBe(true)
    expect(panel.className).not.toContain('translate-x-1/2')
    expect(panel.textContent).toContain('执行证据')
    fireEvent.keyDown(panel, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await waitFor(() => expect(document.activeElement).toBe(trigger))
  })
})
