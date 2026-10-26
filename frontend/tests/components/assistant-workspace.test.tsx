// @vitest-environment jsdom
import { useEffect, useState } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
const mocks = vi.hoisted(() => ({ mounts: vi.fn() }))
vi.mock('@/components/AssistantAvatar', () => ({ AssistantAvatar: () => <span>形象</span> }))
vi.mock('@/pages/AiCopilot', () => ({ default: function MockConversation() {
  const [draft, setDraft] = useState('')
  useEffect(() => { mocks.mounts() }, [])
  return <input aria-label="共享会话草稿" value={draft} onChange={event => setDraft(event.target.value)} />
} }))
import { AssistantWorkspaceProvider, AssistantPageHost } from '@/components/AssistantWorkspace'
import ChatWidget from '@/components/ChatWidget'
function Back() { const navigate = useNavigate(); return <button onClick={() => navigate('/')}>返回首页</button> }
beforeEach(() => { mocks.mounts.mockReset(); window.history.replaceState({}, '', '/') })
afterEach(cleanup)
it('迷你窗切到完整页、再返回迷你窗，保持同一组件实例和草稿', async () => {
  render(<MemoryRouter><AssistantWorkspaceProvider><Back /><Routes><Route path="/" element={<div>首页</div>} /><Route path="/ai-copilot" element={<AssistantPageHost />} /></Routes><ChatWidget /></AssistantWorkspaceProvider></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: '打开 AI 分析助手' }))
  const draft = await screen.findByRole('textbox', { name: '共享会话草稿' })
  fireEvent.change(draft, { target: { value: '保留这段未发送的问题' } })
  fireEvent.click(screen.getByRole('button', { name: '在完整工作台打开' }))
  await waitFor(() => expect(document.querySelector('[data-assistant-page-host] input')).toBeTruthy())
  expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('保留这段未发送的问题')
  fireEvent.click(screen.getByRole('button', { name: '返回首页' }))
  fireEvent.click(await screen.findByRole('button', { name: '打开 AI 分析助手' }))
  await waitFor(() => expect(document.querySelector('[data-assistant-mini-host] input')).toBeTruthy())
  expect((screen.getByRole('textbox') as HTMLInputElement).value).toBe('保留这段未发送的问题')
  expect(mocks.mounts).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: '关闭迷你助手' }))
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: '打开 AI 分析助手' })))
})
