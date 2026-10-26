// @vitest-environment jsdom
/**
 * 模型选择器：生效（PUT 带 model_id）、来源如实、越权只见授权内、失败回滚。
 *
 * 这几条对应产品诚信：显示"跟随场景绑定"时实际不能是偏好模型；保存失败必须回滚显示，
 * 否则用户以为改了、实际没改。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const api = vi.hoisted(() => ({
  fetchAPI: vi.fn(),
}))

vi.mock('@panwatch/api', async () => ({
  ...(await vi.importActual<Record<string, unknown>>('@panwatch/api')),
  ...api,
}))

import { ModelPicker, sourceLabel } from '@/components/copilot/ModelPicker'

const BASE = {
  scene: 'chat',
  current: { source: 'scene', model_id: null, model: 'm-one', service: 'svc-a' },
  models: [
    { id: 1, model: 'm-one', service: 'svc-a', is_default: true },
    { id: 2, model: 'm-two', service: 'svc-a', is_default: false },
  ],
}

beforeEach(() => {
  api.fetchAPI.mockReset()
  api.fetchAPI.mockResolvedValue(BASE)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ModelPicker', () => {
  it('首屏展示当前生效模型与「跟随场景绑定」来源', async () => {
    render(<ModelPicker />)
    await waitFor(() => expect(screen.getByText('m-one')).toBeTruthy())
    expect(screen.getByText('跟随场景绑定')).toBeTruthy()
  })

  it('展开后列出可选模型（含服务商与默认标记）', async () => {
    render(<ModelPicker />)
    await waitFor(() => expect(screen.getByText('m-one')).toBeTruthy())
    fireEvent.click(screen.getByLabelText('选择模型'))
    expect(screen.getByText('m-two')).toBeTruthy()
    expect(screen.getByText(/svc-a · 默认/)).toBeTruthy()
  })

  it('选中模型会 PUT model_id 并显示来源为「你选的」', async () => {
    render(<ModelPicker />)
    await waitFor(() => expect(screen.getByText('m-one')).toBeTruthy())
    fireEvent.click(screen.getByLabelText('选择模型'))

    api.fetchAPI.mockResolvedValueOnce({
      ...BASE,
      current: { source: 'pref', model_id: 2, model: 'm-two', service: 'svc-a' },
    })
    fireEvent.click(screen.getByText('m-two'))

    await waitFor(() => expect(screen.getByText('你选的')).toBeTruthy())
    const put = api.fetchAPI.mock.calls.find((c) => (c[1] as RequestInit | undefined)?.method === 'PUT')
    expect(put).toBeTruthy()
    expect(String(put?.[0])).toBe('/copilot/model')
    expect((put?.[1] as RequestInit).body).toBe(JSON.stringify({ model_id: 2 }))
  })

  it('保存失败要透出服务端 detail 且回滚显示', async () => {
    render(<ModelPicker />)
    await waitFor(() => expect(screen.getByText('m-one')).toBeTruthy())
    fireEvent.click(screen.getByLabelText('选择模型'))
    api.fetchAPI.mockRejectedValueOnce(new Error('模型 2 不在你可用的模型范围内'))
    fireEvent.click(screen.getByText('m-two'))

    await waitFor(() => expect(screen.getByText(/保存失败/)).toBeTruthy())
    expect(screen.getByText(/不在你可用的模型范围内/)).toBeTruthy()
    // 回滚：来源仍是"跟随场景绑定"，不能显示成"你选的"
    expect(screen.queryByText('你选的')).toBeNull()
  })

  it('读取失败显示「读取失败」，不用空下拉冒充', async () => {
    api.fetchAPI.mockRejectedValueOnce(new Error('boom'))
    render(<ModelPicker />)
    await waitFor(() => expect(screen.getByText('读取失败')).toBeTruthy())
  })

  it('空清单显示「没有可用模型」而不是空白', async () => {
    api.fetchAPI.mockResolvedValue({ ...BASE, models: [] })
    render(<ModelPicker />)
    await waitFor(() => expect(screen.getByText('m-one')).toBeTruthy())
    fireEvent.click(screen.getByLabelText('选择模型'))
    expect(screen.getByText(/没有可用模型/)).toBeTruthy()
  })

  it('sourceLabel 未知来源原样返回，不猜', () => {
    expect(sourceLabel('pref')).toBe('你选的')
    expect(sourceLabel('weird_source')).toBe('weird_source')
    expect(sourceLabel(null)).toBe('未知')
  })
})
