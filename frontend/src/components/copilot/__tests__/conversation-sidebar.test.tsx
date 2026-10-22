// @vitest-environment jsdom
/**
 * ConversationSidebar 交互回归。
 *
 * 覆盖六件事（纯展示组件，不发请求）：
 *  1. 列表渲染 + 当前会话高亮（左侧 2px 竖条，不是边框盒子）；
 *  2. 搜索框受控：输入即回调 onQueryChange；
 *  3. 双击标题 → 输入框 → Enter 提交 onRename；Esc 取消；
 *  4. 删除需二次确认（inline，不是 modal）：先点删除，再点确认才回调 onDelete；
 *  5. 置顶图章显示 / 菜单里能切换置顶；
 *  6. 键盘：↑/↓ 切换选中，Enter 选中。
 *
 * 断言风格：只用 vitest 原生 matcher（textContent / className），
 * 不引 jest-dom —— 本仓库 src 下的用例都没挂 jest-dom 扩展（tsc -b 会扫 src）。
 */
import type { ComponentProps } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ConversationSidebar } from '@/components/copilot/ConversationSidebar'
import type { ConversationItem } from '@/hooks/useConversations'

const CONVS: ConversationItem[] = [
  { id: 1, title: '北方稀土底仓', pinned: true, stock_symbol: '600111' },
  { id: 2, title: '神剑股份盘口', pinned: false },
  { id: 3, title: '9:25 爆量抢筹', pinned: false },
]

/** 只有展示态可被用例覆盖；回调一律由本 helper 生成 vi.fn（断言要拿到 mock）。 */
type SidebarOverrides = Partial<
  Pick<ComponentProps<typeof ConversationSidebar>, 'conversations' | 'activeId' | 'query'>
>

function renderSidebar(overrides: SidebarOverrides = {}) {
  const props = {
    conversations: CONVS,
    activeId: 2 as number | null,
    onSelect: vi.fn<(id: number) => void>(),
    onRename: vi.fn<(id: number, title: string) => void>(),
    onPin: vi.fn<(id: number, pinned: boolean) => void>(),
    onDelete: vi.fn<(id: number) => void>(),
    onCreate: vi.fn<() => void>(),
    query: '',
    onQueryChange: vi.fn<(q: string) => void>(),
    ...overrides,
  }
  const utils = render(<ConversationSidebar {...props} />)
  return { props, ...utils }
}

afterEach(() => {
  cleanup()
})

describe('ConversationSidebar 列表渲染', () => {
  it('渲染全部会话标题', () => {
    renderSidebar()
    expect(screen.getByTestId('copilot-conv-title-1').textContent).toContain('北方稀土底仓')
    expect(screen.getByTestId('copilot-conv-title-2').textContent).toContain('神剑股份盘口')
    expect(screen.getByTestId('copilot-conv-title-3').textContent).toContain('9:25 爆量抢筹')
  })

  it('空态显示「还没有对话」+ 一句引导', () => {
    renderSidebar({ conversations: [] })
    const empty = screen.getByTestId('copilot-conv-empty')
    expect(empty.textContent).toContain('还没有对话')
    // 引导句比标题长（不是光秃秃四个字）
    expect((empty.textContent ?? '').trim().length).toBeGreaterThan('还没有对话'.length)
  })

  it('当前会话高亮 = 左侧竖条可见，非当前会话竖条透明', () => {
    renderSidebar({ activeId: 2 })
    expect(screen.getByTestId('copilot-conv-active-bar-2').className).toContain('bg-role-watch')
    expect(screen.getByTestId('copilot-conv-active-bar-1').className).toContain('bg-transparent')
  })

  it('点击标题触发 onSelect', () => {
    const { props } = renderSidebar()
    fireEvent.click(screen.getByTestId('copilot-conv-title-3'))
    expect(props.onSelect).toHaveBeenCalledWith(3)
  })

  it('新建按钮触发 onCreate', () => {
    const { props } = renderSidebar()
    fireEvent.click(screen.getByTestId('copilot-conv-new'))
    expect(props.onCreate).toHaveBeenCalledTimes(1)
  })

  it('未命名会话有兜底标题（不显示空白行）', () => {
    renderSidebar({ conversations: [{ id: 9, title: '' }] })
    expect(screen.getByTestId('copilot-conv-title-9').textContent).toContain('未命名会话')
  })
})

describe('ConversationSidebar 搜索', () => {
  it('输入即回调 onQueryChange（受控，组件自己不持有搜索词）', () => {
    const { props } = renderSidebar()
    const input = screen.getByTestId('copilot-conv-search') as HTMLInputElement
    fireEvent.change(input, { target: { value: '北方' } })
    expect(props.onQueryChange).toHaveBeenCalledWith('北方')
    // 受控：父层不更新时 input 值保持原样（证明没有内部 state 影子）
    expect(input.value).toBe('')
  })
})

describe('ConversationSidebar 重命名', () => {
  it('双击标题变输入框，Enter 提交 onRename', () => {
    const { props } = renderSidebar()
    fireEvent.doubleClick(screen.getByTestId('copilot-conv-title-1'))
    const input = screen.getByTestId('copilot-conv-rename-input-1') as HTMLInputElement
    expect(input).toBeTruthy()
    fireEvent.change(input, { target: { value: '改个名' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(props.onRename).toHaveBeenCalledWith(1, '改个名')
  })

  it('Esc 取消改名（不回调 onRename）', () => {
    const { props } = renderSidebar()
    fireEvent.doubleClick(screen.getByTestId('copilot-conv-title-2'))
    const input = screen.getByTestId('copilot-conv-rename-input-2')
    fireEvent.change(input, { target: { value: '不该保存' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(props.onRename).not.toHaveBeenCalled()
    expect(screen.queryByTestId('copilot-conv-rename-input-2')).toBeNull()
  })

  it('空标题不提交（避免出现空白会话行）', () => {
    const { props } = renderSidebar()
    fireEvent.doubleClick(screen.getByTestId('copilot-conv-title-1'))
    const input = screen.getByTestId('copilot-conv-rename-input-1')
    fireEvent.change(input, { target: { value: '   ' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(props.onRename).not.toHaveBeenCalled()
  })

  it('菜单里的「重命名」也进入编辑态', () => {
    const { props } = renderSidebar()
    fireEvent.click(screen.getByTestId('copilot-conv-menu-btn-2'))
    fireEvent.click(screen.getByTestId('copilot-conv-menu-rename-2'))
    const input = screen.getByTestId('copilot-conv-rename-input-2') as HTMLInputElement
    fireEvent.change(input, { target: { value: '菜单改名' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(props.onRename).toHaveBeenCalledWith(2, '菜单改名')
  })

  it('Enter 后紧跟 blur 只提交一次（改名不被提交两遍）', () => {
    const { props } = renderSidebar()
    fireEvent.doubleClick(screen.getByTestId('copilot-conv-title-1'))
    const input = screen.getByTestId('copilot-conv-rename-input-1')
    fireEvent.change(input, { target: { value: '只提交一次' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.blur(input)
    expect(props.onRename).toHaveBeenCalledTimes(1)
    expect(props.onRename).toHaveBeenCalledWith(1, '只提交一次')
  })
})

describe('ConversationSidebar 菜单 / 置顶 / 删除', () => {
  it('置顶会话显示图章，未置顶不显示', () => {
    renderSidebar()
    expect(screen.queryByTestId('copilot-conv-pin-1')).toBeTruthy()
    expect(screen.queryByTestId('copilot-conv-pin-2')).toBeNull()
  })

  it('菜单里点「置顶」回调 onPin(id, true)', () => {
    const { props } = renderSidebar()
    fireEvent.click(screen.getByTestId('copilot-conv-menu-btn-2'))
    fireEvent.click(screen.getByTestId('copilot-conv-menu-pin-2'))
    expect(props.onPin).toHaveBeenCalledWith(2, true)
  })

  it('已置顶的会话在菜单里是「取消置顶」', () => {
    const { props } = renderSidebar()
    fireEvent.click(screen.getByTestId('copilot-conv-menu-btn-1'))
    fireEvent.click(screen.getByTestId('copilot-conv-menu-pin-1'))
    expect(props.onPin).toHaveBeenCalledWith(1, false)
  })

  it('删除需二次确认：先点删除只出确认框，确认后才回调 onDelete', () => {
    const { props } = renderSidebar()
    fireEvent.click(screen.getByTestId('copilot-conv-menu-btn-3'))
    fireEvent.click(screen.getByTestId('copilot-conv-menu-delete-3'))
    // 第一次点击只是进入确认态，没有删
    expect(props.onDelete).not.toHaveBeenCalled()
    expect(screen.queryByTestId('copilot-conv-confirm-box-3')).toBeTruthy()
    fireEvent.click(screen.getByTestId('copilot-conv-confirm-3'))
    expect(props.onDelete).toHaveBeenCalledWith(3)
  })

  it('取消删除不回调 onDelete', () => {
    const { props } = renderSidebar()
    fireEvent.click(screen.getByTestId('copilot-conv-menu-btn-3'))
    fireEvent.click(screen.getByTestId('copilot-conv-menu-delete-3'))
    fireEvent.click(screen.getByTestId('copilot-conv-cancel-3'))
    expect(props.onDelete).not.toHaveBeenCalled()
  })
})

describe('ConversationSidebar 键盘', () => {
  it('ArrowDown / ArrowUp 切换选中', () => {
    const { props } = renderSidebar({ activeId: 1 })
    const list = screen.getByTestId('copilot-conv-list')
    fireEvent.keyDown(list, { key: 'ArrowDown' })
    expect(props.onSelect).toHaveBeenCalledWith(2)
    fireEvent.keyDown(list, { key: 'ArrowDown' })
    expect(props.onSelect).toHaveBeenLastCalledWith(3)
    fireEvent.keyDown(list, { key: 'ArrowUp' })
    expect(props.onSelect).toHaveBeenLastCalledWith(2)
  })

  it('ArrowUp 不会越过列表顶部', () => {
    const { props } = renderSidebar({ activeId: 1 })
    const list = screen.getByTestId('copilot-conv-list')
    fireEvent.keyDown(list, { key: 'ArrowUp' })
    expect(props.onSelect).toHaveBeenLastCalledWith(1)
  })

  it('ArrowDown 不会越过列表底部', () => {
    const { props } = renderSidebar({ activeId: 3 })
    const list = screen.getByTestId('copilot-conv-list')
    fireEvent.keyDown(list, { key: 'ArrowDown' })
    expect(props.onSelect).toHaveBeenLastCalledWith(3)
  })

  it('Enter 选中当前游标会话', () => {
    const { props } = renderSidebar({ activeId: 1 })
    const list = screen.getByTestId('copilot-conv-list')
    fireEvent.keyDown(list, { key: 'ArrowDown' })
    fireEvent.keyDown(list, { key: 'Enter' })
    expect(props.onSelect).toHaveBeenLastCalledWith(2)
  })

  it('改名输入框里的 Enter 不触发列表选中（改标题不是切会话）', () => {
    const { props } = renderSidebar()
    fireEvent.doubleClick(screen.getByTestId('copilot-conv-title-1'))
    const input = screen.getByTestId('copilot-conv-rename-input-1')
    props.onSelect.mockClear()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(props.onSelect).not.toHaveBeenCalled()
  })
})
