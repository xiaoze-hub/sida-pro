// @vitest-environment jsdom
import { useState } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import PageTabs from '@/components/PageTabs'

afterEach(cleanup)
const tabs = [{ key: 'price', label: '价格结构' }, { key: 'volume', label: '量价分析' }, { key: 'sources', label: '数据来源' }]
function Example() {
  const [active, setActive] = useState('price')
  return <><PageTabs tabs={tabs} value={active} onChange={setActive} idPrefix="research" panelId="research-panel" /><div role="tabpanel" id="research-panel" aria-labelledby={`research-${active}`}>{active}</div></>
}

describe('页面内页签键盘可达', () => {
  it('左右键循环选择，移动焦点，同时更新当前面板', () => {
    render(<Example />)
    fireEvent.keyDown(screen.getByRole('tab', { name: '价格结构' }), { key: 'ArrowRight' })
    const volume = screen.getByRole('tab', { name: '量价分析' })
    expect(document.activeElement).toBe(volume)
    expect(volume.getAttribute('aria-selected')).toBe('true')
    expect(volume.getAttribute('aria-controls')).toBe('research-panel')
    expect(screen.getByRole('tabpanel').textContent).toBe('volume')
    fireEvent.keyDown(volume, { key: 'ArrowLeft' })
    fireEvent.keyDown(screen.getByRole('tab', { name: '价格结构' }), { key: 'ArrowLeft' })
    expect(document.activeElement).toBe(screen.getByRole('tab', { name: '数据来源' }))
  })
  it('Home/End 到首尾，只有当前页签进入自然 Tab 顺序', () => {
    render(<Example />)
    fireEvent.keyDown(screen.getByRole('tab', { name: '价格结构' }), { key: 'End' })
    expect(screen.getByRole('tab', { name: '数据来源' }).tabIndex).toBe(0)
    expect(screen.getByRole('tab', { name: '价格结构' }).tabIndex).toBe(-1)
    fireEvent.keyDown(document.activeElement!, { key: 'Home' })
    expect(screen.getByRole('tab', { name: '价格结构' }).tabIndex).toBe(0)
  })
})
