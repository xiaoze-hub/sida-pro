// @vitest-environment jsdom
/**
 * 经验库页面测试（P2-2.4）。
 *
 * 钉住三件容易出错的事：
 *  1. 缺值不补 0 —— baseline 没有的键、没有的数字，必须显示 `--`；
 *  2. 失败态要透出服务端原文，不能笼统写"加载失败"；
 *  3. 证据文件缺失要明说「证据文件缺失」，不给假下载入口。
 */
import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const fetchAPIMock = vi.fn()
vi.mock('@panwatch/api', () => ({
  fetchAPI: (...args: unknown[]) => fetchAPIMock(...args),
}))

import ExperimentsPage from '../../src/pages/Experiments'

const listOk = {
  available: true,
  items: [
    { id: 1, recipe_id: 'deep_diagnosis', change_summary: '补一句必须带口径', verdict: 'keep', created_at: '2026-10-25 10:00' },
    { id: 2, recipe_id: 'theme_leader', change_summary: '删掉重复取数', verdict: 'rollback', created_at: '2026-10-24 09:00' },
  ],
  total: 2,
  stats: { keep: 1, rollback: 1, total: 2 },
}

/** 按 URL 分发：列表 vs 详情，互不干扰。 */
function wire(opts: { list?: unknown; detail?: unknown; listError?: Error; detailError?: Error }) {
  fetchAPIMock.mockImplementation((url: string) => {
    const isDetail = /\/experiments\/\d+/.test(url)
    if (isDetail) {
      if (opts.detailError) return Promise.reject(opts.detailError)
      return Promise.resolve(opts.detail ?? { available: true, item: null })
    }
    if (opts.listError) return Promise.reject(opts.listError)
    return Promise.resolve(opts.list ?? listOk)
  })
}

beforeEach(() => {
  fetchAPIMock.mockReset()
})

// 本仓 vitest 关了 globals，Testing Library 不会自动 cleanup —— 不手动清会出现
// 上一个用例的 DOM 残留，导致「Found multiple elements」误报。
afterEach(() => {
  cleanup()
})

describe('经验库页面', () => {
  it('有数据时渲染表格与统计', async () => {
    wire({})
    render(<ExperimentsPage />)
    await waitFor(() => expect(screen.getByTestId('experiments-page')).toBeTruthy())
    expect(await screen.findByText('补一句必须带口径')).toBeTruthy()
    expect(screen.getByText('保留')).toBeTruthy()
    expect(screen.getByText('回滚')).toBeTruthy()
    expect(screen.getByTestId('experiments-stats').textContent).toContain('共 2 条')
  })

  it('无数据给空态，不是空表格', async () => {
    wire({ list: { available: true, items: [], total: 0, stats: {} } })
    render(<ExperimentsPage />)
    expect(await screen.findByTestId('experiments-empty')).toBeTruthy()
  })

  it('服务端不可用时透出原文', async () => {
    wire({ list: { available: false, reason: 'verdict 只能是 keep/rollback', items: [], total: 0 } })
    render(<ExperimentsPage />)
    const box = await screen.findByTestId('experiments-error')
    expect(box.textContent).toContain('verdict 只能是 keep/rollback')
  })

  it('网络异常也要显示原因', async () => {
    wire({ listError: new Error('502 Bad Gateway') })
    render(<ExperimentsPage />)
    const box = await screen.findByTestId('experiments-error')
    expect(box.textContent).toContain('502 Bad Gateway')
  })

  it('展开详情：缺值显示 --，不补 0', async () => {
    wire({})
    render(<ExperimentsPage />)
    await screen.findByText('补一句必须带口径')
    wire({
      detail: {
        available: true,
        item: {
          ...listOk.items[0],
          baseline: { pass_rate: 0.5, cost: null },
          candidate: { pass_rate: 0.75 },
          reasons: [],
          evidence_path: '/tmp/compare-1.json',
          evidence_exists: false,
        },
      },
    })
    fireEvent.click(screen.getByTestId('experiment-row-1'))
    const detail = await screen.findByTestId('experiment-detail-1')
    expect(detail.textContent).toContain('0.5')
    expect(detail.textContent).toContain('0.75')
    expect(detail.textContent).toContain('--')
    expect(detail.textContent).not.toContain('0.00')
  })

  it('证据文件缺失要明说', async () => {
    wire({})
    render(<ExperimentsPage />)
    await screen.findByText('补一句必须带口径')
    wire({
      detail: {
        available: true,
        item: {
          ...listOk.items[0],
          baseline: {}, candidate: {}, reasons: [],
          evidence_path: '/tmp/compare-1.json', evidence_exists: false,
        },
      },
    })
    fireEvent.click(screen.getByTestId('experiment-row-1'))
    const detail = await screen.findByTestId('experiment-detail-1')
    expect(detail.textContent).toContain('证据文件缺失')
  })

  it('回滚原因要逐条显示', async () => {
    wire({})
    render(<ExperimentsPage />)
    await screen.findByText('补一句必须带口径')
    wire({
      detail: {
        available: true,
        item: { ...listOk.items[1], baseline: {}, candidate: {}, reasons: ['pass_rate 下降', 'cost 上升'] },
      },
    })
    fireEvent.click(screen.getByTestId('experiment-row-2'))
    const detail = await screen.findByTestId('experiment-detail-2')
    expect(detail.textContent).toContain('pass_rate 下降')
    expect(detail.textContent).toContain('cost 上升')
  })
})
