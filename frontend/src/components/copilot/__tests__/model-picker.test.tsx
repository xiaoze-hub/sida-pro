// @vitest-environment jsdom
/**
 * ModelPicker 下拉契约测试 — 2026-10-27 修「列表被上面挡住, 看不到模型名称」
 *
 * 原实现是 `absolute bottom-full`（向上弹）挂在顶部工具条里, 工具条靠近视口顶部时整块面板
 * 翻到视口外, 用户只能看到被裁掉的一条。现在改用 Radix Popover:
 *  ① **Portal 到 body**: 面板不能留在触发器的祖先容器里（那样任何 overflow 都会裁它）；
 *  ② 触发器要有 aria-expanded, 模型名与服务名都在面板里可读；
 *  ③ 选中后回调 PUT 并收起（老逻辑不能因为换组件而丢）。
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const fetchAPI = vi.fn()

vi.mock('@sida/api', () => ({
  fetchAPI: (...args: unknown[]) => fetchAPI(...args),
}))

import { ModelPicker } from '../ModelPicker'

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const STATE = {
  scene: 'chat',
  current: { source: 'pref', model_id: 1, model: 'deepseek-v4.1-flash-1017', service: 'custom' },
  models: [
    { id: 1, model: 'deepseek-v4.1-flash-1017', service: 'custom', is_default: true },
    { id: 2, model: 'glm-5.3', service: 'tokenrhythm', is_default: false },
  ],
}

beforeEach(() => {
  ;(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = ResizeObserverStub
  fetchAPI.mockReset()
  fetchAPI.mockResolvedValue(STATE)
})

afterEach(cleanup)

describe('ModelPicker 下拉不被顶部裁掉', () => {
  it('面板 Portal 到 body, 不在触发器祖先里(祖先 overflow 裁不到它)', async () => {
    render(<ModelPicker />)
    const trigger = await screen.findByRole('button', { name: '选择模型' })
    fireEvent.click(trigger)

    const menu = await screen.findByTestId('copilot-model-menu')
    expect(menu.closest('[data-testid="copilot-model-picker"]')).toBeNull()
    expect(document.body.contains(menu)).toBe(true)
    expect(trigger.getAttribute('aria-expanded')).toBe('true')
  })

  it('面板里能读到完整模型名与服务名(不是只剩图标)', async () => {
    render(<ModelPicker />)
    fireEvent.click(await screen.findByRole('button', { name: '选择模型' }))
    const menu = await screen.findByTestId('copilot-model-menu')
    expect(menu.textContent).toContain('deepseek-v4.1-flash-1017')
    expect(menu.textContent).toContain('glm-5.3')
    expect(menu.textContent).toContain('tokenrhythm')
    expect(menu.textContent).toContain('跟随场景绑定')
  })

  it('选中模型后 PUT /copilot/model 并收起面板', async () => {
    render(<ModelPicker />)
    fireEvent.click(await screen.findByRole('button', { name: '选择模型' }))
    const menu = await screen.findByTestId('copilot-model-menu')
    const target = Array.from(menu.querySelectorAll('button')).find((b) => b.textContent?.includes('glm-5.3'))
    expect(target).toBeTruthy()
    fireEvent.click(target!)
    await waitFor(() => {
      expect(fetchAPI).toHaveBeenCalledWith(
        '/copilot/model',
        expect.objectContaining({ method: 'PUT', body: JSON.stringify({ model_id: 2 }) }),
      )
    })
    await waitFor(() => expect(screen.queryByTestId('copilot-model-menu')).toBeNull())
  })

  it('保存失败时回滚显示并给出服务端 detail', async () => {
    fetchAPI.mockResolvedValueOnce(STATE).mockRejectedValueOnce(new Error('模型未授权'))
    render(<ModelPicker />)
    fireEvent.click(await screen.findByRole('button', { name: '选择模型' }))
    const menu = await screen.findByTestId('copilot-model-menu')
    const target = Array.from(menu.querySelectorAll('button')).find((b) => b.textContent?.includes('glm-5.3'))
    fireEvent.click(target!)
    await screen.findByText(/模型未授权/)
    const trigger = screen.getByRole('button', { name: '选择模型' })
    expect(trigger.textContent).toContain('deepseek-v4.1-flash-1017')
  })
})
