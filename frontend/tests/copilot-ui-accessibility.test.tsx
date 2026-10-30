// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
afterEach(cleanup)
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '../packages/base-ui/src/components/ui/collapsible'
import { Tooltip, TooltipTrigger, TooltipContent } from '../packages/base-ui/src/components/ui/tooltip'

describe('参谋基础组件可访问性', () => {
  it('自定义触发器转发属性并尊重阻止默认行为', () => {
    render(<Collapsible><CollapsibleTrigger asChild id="control" onClick={e => e.preventDefault()}><button>开关</button></CollapsibleTrigger><CollapsibleContent>内容</CollapsibleContent></Collapsible>)
    const trigger = screen.getByRole('button', { name: '开关' })
    expect(trigger.id).toBe('control')
    fireEvent.click(trigger)
    expect(trigger.getAttribute('aria-expanded')).toBe('false')
  })
  it('保持挂载的折叠内容关闭时不可聚焦且不对辅助技术可见', () => {
    render(<Collapsible open={false}><CollapsibleContent forceMount><button>内部操作</button></CollapsibleContent></Collapsible>)
    expect(screen.queryByRole('button', { name: '内部操作' })).toBeNull()
  })
  it('打开提示时不抢占输入框焦点', () => {
    const { rerender } = render(<><input aria-label="输入" /><Tooltip open={false}><TooltipTrigger>查看焦点</TooltipTrigger><TooltipContent>提示</TooltipContent></Tooltip></>)
    const input = screen.getByRole('textbox', { name: '输入' })
    input.focus()
    rerender(<><input aria-label="输入" /><Tooltip open><TooltipTrigger>查看焦点</TooltipTrigger><TooltipContent>提示</TooltipContent></Tooltip></>)
    expect(document.activeElement).toBe(input)
  })
  it('提示内容具有 tooltip 语义且关联触发元素', () => {
    render(<Tooltip defaultOpen><TooltipTrigger>查看</TooltipTrigger><TooltipContent>提示详情</TooltipContent></Tooltip>)
    const tip = screen.getByRole('tooltip')
    expect(screen.getByRole('button', { name: '查看' }).getAttribute('aria-describedby')).toBe(tip.id)
  })
})
