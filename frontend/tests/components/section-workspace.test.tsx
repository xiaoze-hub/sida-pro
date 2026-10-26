// @vitest-environment jsdom
import { fireEvent, render, screen, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { SectionWorkspace, SectionPanel, useSectionRoute } from '@/components/SectionWorkspace'
const items = [{ id: 'account', label: '账户' }, { id: 'notify', label: '通知' }]
function Example() {
  const [value, select] = useSectionRoute(items, 'account')
  const location = useLocation()
  return <><SectionWorkspace items={items} value={value} onChange={select} label="测试分类"><SectionPanel id="account"><p>账户内容</p></SectionPanel><SectionPanel id="notify"><p>通知内容</p></SectionPanel><SectionPanel id="owner"><p>无权限的管理内容</p></SectionPanel></SectionWorkspace><output>{location.search}</output></>
}
afterEach(cleanup)
describe('分类工作区', () => {
  it('只挂载当前分类，未提供的权限分类不渲染', () => {
    render(<MemoryRouter><Example /></MemoryRouter>)
    expect(screen.getByText('账户内容')).toBeTruthy()
    expect(screen.queryByText('通知内容')).toBeNull()
    expect(screen.queryByText('无权限的管理内容')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '通知' }))
    expect(screen.queryByText('账户内容')).toBeNull()
    expect(screen.getByText('通知内容')).toBeTruthy()
  })
  it('分类深链保留父级 tab，手机选择器也切换同一状态', () => {
    render(<MemoryRouter initialEntries={['/settings?tab=settings&section=notify']}><Example /></MemoryRouter>)
    expect(screen.getByText('通知内容')).toBeTruthy()
    expect(screen.getByRole('button', { name: '通知' }).getAttribute('aria-current')).toBe('page')
    fireEvent.change(screen.getByRole('combobox', { name: '测试分类' }), { target: { value: 'account' } })
    expect(screen.getByRole('status').textContent).toContain('tab=settings')
    expect(screen.getByRole('status').textContent).toContain('section=account')
  })
})
