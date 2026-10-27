// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { AssistantWorkspaceProvider } from '@/components/AssistantWorkspace'
import ChatWidget from '@/components/ChatWidget'

vi.mock('@/components/AssistantAvatar', () => ({ AssistantAvatar: () => <span>形象</span> }))
vi.mock('@/pages/AiCopilot', () => ({ default: () => <textarea aria-label="分析问题" /> }))

let userSequence = 0
function renderWidget() {
  return render(<MemoryRouter><AssistantWorkspaceProvider><ChatWidget /></AssistantWorkspaceProvider></MemoryRouter>)
}

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('user', JSON.stringify({ id: `greeting-test-${++userSequence}` }))
  window.history.replaceState({}, '', '/')
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

it('关闭仅隐藏文字气泡，停止轮换，保留可用入口和键盘焦点', async () => {
  vi.useFakeTimers()
  renderWidget()
  const launcher = screen.getByTestId('assistant-launcher')
  fireEvent.click(screen.getByRole('button', { name: '不再显示 AI 引导气泡' }))
  expect(screen.queryByTestId('assistant-greeting')).toBeNull()
  expect(screen.queryByTestId('assistant-mini-window')).toBeNull()
  expect(document.activeElement).toBe(launcher)
  act(() => vi.advanceTimersByTime(60_000))
  expect(screen.queryByTestId('assistant-greeting')).toBeNull()
  vi.useRealTimers()
  fireEvent.click(launcher)
  expect(await screen.findByRole('textbox', { name: '分析问题' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '关闭迷你助手' }))
  expect(screen.getByTestId('assistant-launcher')).toBeTruthy()
  expect(screen.queryByTestId('assistant-greeting')).toBeNull()
})

it('从持久化偏好恢复时首帧就隐藏，不闪现气泡', () => {
  const user = JSON.parse(localStorage.getItem('user')!)
  localStorage.setItem(`sida_assistant_greeting_dismissed:${user.id}`, '1')
  renderWidget()
  expect(screen.queryByTestId('assistant-greeting')).toBeNull()
  expect(screen.getByTestId('assistant-launcher')).toBeTruthy()
})

it('按账号保存关闭状态，切换账号不会共用关闭偏好', () => {
  const firstUser = localStorage.getItem('user')!
  const widget = renderWidget()
  fireEvent.click(screen.getByRole('button', { name: '不再显示 AI 引导气泡' }))
  const id = JSON.parse(firstUser).id
  expect(localStorage.getItem(`sida_assistant_greeting_dismissed:${id}`)).toBe('1')
  localStorage.setItem('user', JSON.stringify({ id: `${id}-second` }))
  widget.rerender(<MemoryRouter><AssistantWorkspaceProvider><ChatWidget /></AssistantWorkspaceProvider></MemoryRouter>)
  expect(screen.getByTestId('assistant-greeting')).toBeTruthy()
  localStorage.setItem('user', firstUser)
  widget.rerender(<MemoryRouter><AssistantWorkspaceProvider><ChatWidget /></AssistantWorkspaceProvider></MemoryRouter>)
  expect(screen.queryByTestId('assistant-greeting')).toBeNull()
})

it('存储写入被拒绝时，重新挂载入口也保留本窗口的关闭状态', () => {
  const widget = renderWidget()
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('Storage denied') })
  fireEvent.click(screen.getByRole('button', { name: '不再显示 AI 引导气泡' }))
  widget.unmount()
  renderWidget()
  expect(screen.queryByTestId('assistant-greeting')).toBeNull()
  expect(screen.getByTestId('assistant-launcher')).toBeTruthy()
})

it('点击引导正文仍打开同一个迷你助手', async () => {
  renderWidget()
  fireEvent.click(screen.getByRole('button', { name: '查看数智参谋引导' }))
  expect(await screen.findByRole('textbox', { name: '分析问题' })).toBeTruthy()
  expect(screen.getAllByTestId('assistant-mini-window')).toHaveLength(1)
})
