// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { AssistantConversationLoader } from '@/components/AssistantConversationLoader'

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })

it('慢加载提供重试，重新调用加载器后进入参谋', async () => {
  vi.useFakeTimers()
  const loader = vi.fn()
    .mockImplementationOnce(() => new Promise(() => {}))
    .mockResolvedValueOnce({ default: () => <div>参谋已就绪</div> })
  render(<AssistantConversationLoader loader={loader} />)
  expect(screen.getByRole('status').textContent).toContain('正在打开')
  await act(async () => { vi.advanceTimersByTime(12000) })
  expect(screen.getByRole('status').textContent).toContain('加载时间较长')
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '重新加载参谋' })) })
  expect(loader).toHaveBeenCalledTimes(2)
  expect(screen.getByText('参谋已就绪')).toBeTruthy()
})

it('加载异常后重试使用新的 lazy 实例', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {})
  const loader = vi.fn()
    .mockRejectedValueOnce(new Error('test failure'))
    .mockResolvedValueOnce({ default: () => <div>参谋已就绪</div> })
  render(<AssistantConversationLoader loader={loader} />)
  const retry = await screen.findByRole('button', { name: '重新加载参谋' })
  fireEvent.click(retry)
  expect(await screen.findByText('参谋已就绪')).toBeTruthy()
  expect(loader).toHaveBeenCalledTimes(2)
})
