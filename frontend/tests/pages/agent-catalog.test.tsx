// @vitest-environment jsdom
/**
 * Agent 目录页（P2-2.1）。
 *
 * 钉住：版本号缺失显示 `--`（不补 0）、失败态透出服务端原文、历史为空不编"上一版"。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const fetchAPIMock = vi.fn()
vi.mock('@sida/api', () => ({
  fetchAPI: (...args: unknown[]) => fetchAPIMock(...args),
}))

import AgentCatalogPage from '../../src/pages/AgentCatalog'

const listOk = {
  available: true,
  count: 2,
  items: [
    { id: 'deep_diagnosis', label: '全景诊断', icon: '🔬', desc: '六层时间轴', when: ['怎么样'],
      tools: ['get_trend_profile', 'get_chip'], output_sections: ['结论', '依据'], version: 'f8a6a643',
      updated_at: '2026-10-23T00:25:57' },
    { id: 'theme_leader', label: '题材龙头', icon: '🎯', desc: '谁是龙头', when: ['龙头'],
      tools: ['get_hot_stocks'], output_sections: ['结论'], version: null, updated_at: null },
  ],
}

function wire(opts: { list?: unknown; detail?: unknown; listError?: Error; detailError?: Error }) {
  fetchAPIMock.mockImplementation((url: string) => {
    const isDetail = /\/agents\/[^/]+$/.test(url)
    if (isDetail) {
      if (opts.detailError) throw opts.detailError
      return Promise.resolve(opts.detail ?? { available: true, item: null, history: [] })
    }
    // 同步抛而不是 Promise.reject：reject 在组件卸载后会给 vitest 报未处理拒绝，
    // 页面的 try/catch 对两种都有效，用同步抛能把断言聚焦在 UI 上
    if (opts.listError) throw opts.listError
    return Promise.resolve(opts.list ?? listOk)
  })
}

beforeEach(() => fetchAPIMock.mockReset())
afterEach(() => cleanup())

describe('Agent 目录页', () => {
  it('列出全部配方与版本号', async () => {
    wire({})
    render(<AgentCatalogPage />)
    expect(await screen.findByTestId('agent-catalog-page')).toBeTruthy()
    expect(await screen.findByText('全景诊断')).toBeTruthy()
    expect(screen.getByTestId('agent-row-deep_diagnosis').textContent).toContain('f8a6a643')
  })

  it('版本缺失显示 -- 而不是 0', async () => {
    wire({})
    render(<AgentCatalogPage />)
    const row = await screen.findByTestId('agent-row-theme_leader')
    expect(row.textContent).toContain('--')
    expect(row.textContent).not.toContain('0 工具 · 0')
  })

  it('空目录给空态', async () => {
    wire({ list: { available: true, items: [], count: 0 } })
    render(<AgentCatalogPage />)
    expect(await screen.findByTestId('agent-catalog-empty')).toBeTruthy()
  })

  it('服务端不可用透出原文', async () => {
    wire({ list: { available: false, reason: '配方目录为空', items: [], count: 0 } })
    render(<AgentCatalogPage />)
    expect((await screen.findByTestId('agent-catalog-error')).textContent).toContain('配方目录为空')
  })

  it('响应缺字段时进错误态并给出原因', async () => {
    // 这里刻意不测 Promise.reject：组件卸载后的拒绝会被 vitest 记为未处理拒绝，
    // 掩盖真正的断言。用"返回了坏数据"走同一条 catch 分支，断言更聚焦。
    wire({ list: {} })
    render(<AgentCatalogPage />)
    const box = await screen.findByTestId('agent-catalog-error')
    expect(box.textContent).toContain('服务端未给出原因')
  })

  it('展开详情显示工具与历史', async () => {
    wire({ detail: { available: true, item: listOk.items[0],
      history: [{ fingerprint: 'f8a6a643', created_at: '2026-10-23 00:25:57' }] } })
    render(<AgentCatalogPage />)
    await screen.findByText('全景诊断')
    fireEvent.click(screen.getByTestId('agent-row-deep_diagnosis'))
    const d = await screen.findByTestId('agent-detail-deep_diagnosis')
    expect(d.textContent).toContain('get_trend_profile')
    expect(d.textContent).toContain('f8a6a643')
  })

  it('无历史时说明只有一次观察，不编版本', async () => {
    wire({ detail: { available: true, item: listOk.items[0], history: [] } })
    render(<AgentCatalogPage />)
    await screen.findByText('全景诊断')
    fireEvent.click(screen.getByTestId('agent-row-deep_diagnosis'))
    const d = await screen.findByTestId('agent-detail-deep_diagnosis')
    await waitFor(() => expect(d.textContent).toContain('只有当前一次观察记录'))
  })
})
