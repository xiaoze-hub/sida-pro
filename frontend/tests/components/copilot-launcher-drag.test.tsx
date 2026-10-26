// @vitest-environment jsdom
/**
 * 悬浮球拖拽 + 迷你窗全屏（2026-10-26 产品要求）。
 *
 * 这些用例钉的是**行为**，不是实现细节：
 *  · 拖动后按钮真的改变了位置（left/top 内联值）；
 *  · 拖拽结束补的那个 click 不能顺带把面板打开（真实用户最烦的误触）；
 *  · 纯点击仍然打开面板（键盘 Enter/Space 也走同一条路）；
 *  · 全屏开关切换 data-fullscreen；Esc 先退全屏、再按一次才关窗。
 */
import { useState } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/components/AssistantAvatar', () => ({ AssistantAvatar: () => <span>形象</span> }))
vi.mock('@/pages/AiCopilot', () => ({
  default: function MockConversation() {
    const [draft, setDraft] = useState('')
    return <input aria-label="共享会话草稿" value={draft} onChange={event => setDraft(event.target.value)} />
  },
}))

import { AssistantWorkspaceProvider } from '@/components/AssistantWorkspace'
import ChatWidget from '@/components/ChatWidget'
import { LAUNCHER_POS_KEY } from '@/lib/copilot/draggable'

function renderWidget() {
  return render(
    <MemoryRouter>
      <AssistantWorkspaceProvider>
        <ChatWidget />
      </AssistantWorkspaceProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  window.localStorage.clear()
  window.history.replaceState({}, '', '/')
})

afterEach(cleanup)

describe('悬浮球拖拽', () => {
  it('标记为可拖动，并给出位置提示', () => {
    renderWidget()
    const launcher = screen.getByTestId('assistant-launcher')
    expect(launcher.dataset.draggable).toBe('true')
    expect(launcher.getAttribute('title')).toContain('拖动可移动')
  })

  it('拖动后位置改变并写入 localStorage', async () => {
    renderWidget()
    const launcher = screen.getByTestId('assistant-launcher')
    Object.defineProperty(launcher, 'offsetWidth', { value: 80, configurable: true })
    Object.defineProperty(launcher, 'offsetHeight', { value: 80, configurable: true })

    fireEvent.pointerDown(launcher, { pointerId: 1, clientX: 900, clientY: 600, button: 0 })
    fireEvent.pointerMove(launcher, { pointerId: 1, clientX: 620, clientY: 300 })
    fireEvent.pointerUp(launcher, { pointerId: 1, clientX: 620, clientY: 300 })

    await waitFor(() => {
      const stored = window.localStorage.getItem(LAUNCHER_POS_KEY)
      expect(stored).toBeTruthy()
      const parsed = JSON.parse(stored!) as { x: number; y: number }
      // 位移量被如实应用（起点 900,600 → 终点 620,300，左移 280 / 上移 300）
      expect(parsed.x).toBeLessThan(900)
      expect(parsed.y).toBeLessThan(600)
    })
  })

  it('拖拽后的补发 click 不会打开面板', async () => {
    renderWidget()
    const launcher = screen.getByTestId('assistant-launcher')
    fireEvent.pointerDown(launcher, { pointerId: 2, clientX: 800, clientY: 500, button: 0 })
    fireEvent.pointerMove(launcher, { pointerId: 2, clientX: 500, clientY: 200 })
    fireEvent.pointerUp(launcher, { pointerId: 2, clientX: 500, clientY: 200 })
    fireEvent.click(launcher)

    expect(screen.queryByTestId('assistant-mini-window')).toBeNull()
  })

  it('没拖动的点击照旧打开面板', async () => {
    renderWidget()
    const launcher = screen.getByTestId('assistant-launcher')
    fireEvent.pointerDown(launcher, { pointerId: 3, clientX: 800, clientY: 500, button: 0 })
    fireEvent.pointerUp(launcher, { pointerId: 3, clientX: 801, clientY: 500 })
    fireEvent.click(launcher)

    expect(await screen.findByTestId('assistant-mini-window')).toBeTruthy()
  })
})

describe('迷你窗全屏', () => {
  async function openWindow() {
    renderWidget()
    fireEvent.click(screen.getByTestId('assistant-launcher'))
    return screen.findByTestId('assistant-mini-window')
  }

  it('可在全屏与浮窗之间切换', async () => {
    const panel = await openWindow()
    expect(panel.getAttribute('data-fullscreen')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '全屏显示' }))
    await waitFor(() => expect(screen.getByTestId('assistant-mini-window').getAttribute('data-fullscreen')).toBe('true'))
    expect(screen.getByTestId('assistant-mini-window').className).toContain('rounded-none')

    fireEvent.click(screen.getByRole('button', { name: '退出全屏' }))
    await waitFor(() => expect(screen.getByTestId('assistant-mini-window').getAttribute('data-fullscreen')).toBeNull())
  })

  it('Esc 先退全屏，再按一次才关窗', async () => {
    await openWindow()
    fireEvent.click(screen.getByRole('button', { name: '全屏显示' }))
    await waitFor(() => expect(screen.getByTestId('assistant-mini-window').getAttribute('data-fullscreen')).toBe('true'))

    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.getByTestId('assistant-mini-window').getAttribute('data-fullscreen')).toBeNull())

    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('assistant-mini-window')).toBeNull())
  })
})
