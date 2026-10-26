// @vitest-environment jsdom
/**
 * MemoryPanel 行为回归（2026-10-22）。
 *
 * 守四件事：
 *  ① 列表能渲染（内容 / 类型徽标 / 权重 / 命中次数）；
 *  ② 新增走 POST 且**乐观插入**（不等服务端回来就上屏）；
 *  ③ 启停走 PATCH；删除有**二次确认**；
 *  ④ 空态文案固定。
 *
 * 网络一律 mock（`fetch` 打桩），不连真后端。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import { MemoryPanel } from '@/components/copilot/MemoryPanel'

interface FetchCall {
  url: string
  method: string
  body: string | null
}

const calls: FetchCall[] = []
let memoryRows: unknown[] = []

function jsonResponse(data: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    json: async () => ({ code: ok ? 0 : 500, success: ok, data, message: ok ? 'ok' : 'err' }),
  } as unknown as Response
}

beforeEach(() => {
  calls.length = 0
  memoryRows = [
    {
      id: 1,
      kind: 'preference',
      content: '我只做主板，不碰北交所',
      weight: 7,
      source: 'user',
      enabled: true,
      hits: 3,
      created_at: '2026-10-21 10:00:00',
    },
    {
      id: 2,
      kind: 'rule',
      content: '资金流别用东财方向位回答方向',
      weight: 10,
      source: 'auto',
      enabled: true,
      hits: 0,
      created_at: '2026-10-21 11:00:00',
    },
  ]
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? 'GET').toUpperCase()
    calls.push({
      url: String(url),
      method,
      body: typeof init?.body === 'string' ? init.body : null,
    })
    if (method === 'GET') return jsonResponse(memoryRows)
    if (method === 'POST') {
      const parsed = JSON.parse(String(init?.body || '{}'))
      const created = {
        id: 99,
        kind: parsed.kind,
        content: parsed.content,
        weight: parsed.weight,
        source: 'user',
        enabled: true,
        hits: 0,
        created_at: '2026-10-22 09:00:00',
      }
      memoryRows = [created, ...memoryRows]
      return jsonResponse(created)
    }
    return jsonResponse({ ok: true })
  }))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('MemoryPanel', () => {
  it('渲染两条记忆：内容 / 类型徽标 / 权重 / 命中次数', async () => {
    render(<MemoryPanel />)
    await waitFor(() => expect(screen.getAllByTestId('memory-row')).toHaveLength(2))

    expect(screen.getByText('我只做主板，不碰北交所')).toBeTruthy()
    expect(screen.getByText('资金流别用东财方向位回答方向')).toBeTruthy()
    // 类型徽标
    const badges = screen.getAllByTestId('memory-kind-badge').map((el) => el.textContent)
    expect(badges).toEqual(['偏好', '规则'])
    // 权重 + 命中次数
    expect(screen.getByText('权重 7')).toBeTruthy()
    expect(screen.getByText('权重 10')).toBeTruthy()
    expect(screen.getByText('命中 3 次')).toBeTruthy()
    // 命中 0 是**真实 0**，不是缺数 → 显示 0 而不是 --
    expect(screen.getByText('命中 0 次')).toBeTruthy()
    expect(screen.queryByText('--')).toBeNull()
  })

  it('空态文案固定', async () => {
    memoryRows = []
    render(<MemoryPanel />)
    const empty = await screen.findByTestId('memory-empty')
    expect(empty.textContent).toContain('还没有记忆')
    expect(empty.textContent).toContain('记住：')
  })

  it('新增：调 POST 且乐观插入', async () => {
    render(<MemoryPanel />)
    await waitFor(() => expect(screen.getAllByTestId('memory-row')).toHaveLength(2))

    fireEvent.change(screen.getByTestId('memory-content-input'), {
      target: { value: '新记忆：只用筹码口径' },
    })
    fireEvent.change(screen.getByTestId('memory-kind-select'), { target: { value: 'rule' } })
    fireEvent.change(screen.getByTestId('memory-weight-input'), { target: { value: '9' } })
    fireEvent.click(screen.getByTestId('memory-add-btn'))

    // POST 已发出，body 三字段齐全
    await waitFor(() => {
      const post = calls.find((c) => c.method === 'POST')
      expect(post).toBeTruthy()
      expect(post?.url).toContain('/api/copilot/memory')
      const body = JSON.parse(String(post?.body))
      expect(body.content).toBe('新记忆：只用筹码口径')
      expect(body.kind).toBe('rule')
      expect(body.weight).toBe(9)
    })

    // 乐观插入：内容立刻上屏（不等服务端返回的 id）
    await waitFor(() => expect(screen.getAllByTestId('memory-row')).toHaveLength(3))
    expect(screen.getByText('新记忆：只用筹码口径')).toBeTruthy()
  })

  it('新增失败：乐观行被撤掉', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      const method = (init?.method ?? 'GET').toUpperCase()
      calls.push({ url: String(url), method, body: typeof init?.body === 'string' ? init.body : null })
      if (method === 'GET') return jsonResponse(memoryRows)
      return jsonResponse(null, false)
    }))
    render(<MemoryPanel />)
    await waitFor(() => expect(screen.getAllByTestId('memory-row')).toHaveLength(2))

    fireEvent.change(screen.getByTestId('memory-content-input'), { target: { value: '写不进去的' } })
    fireEvent.click(screen.getByTestId('memory-add-btn'))

    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    await waitFor(() => expect(screen.getAllByTestId('memory-row')).toHaveLength(2))
    expect(screen.queryByText('写不进去的')).toBeNull()
  })

  it('开关：调 PATCH 且带上 enabled', async () => {
    render(<MemoryPanel />)
    await waitFor(() => expect(screen.getAllByTestId('memory-row')).toHaveLength(2))

    const toggles = screen.getAllByTestId('memory-toggle')
    fireEvent.click(toggles[0])

    await waitFor(() => {
      const patch = calls.find((c) => c.method === 'PATCH')
      expect(patch).toBeTruthy()
      expect(patch?.url).toContain('/api/copilot/memory/1')
      expect(JSON.parse(String(patch?.body)).enabled).toBe(false)
    })
  })

  it('删除：先二次确认，确认后才调 DELETE', async () => {
    render(<MemoryPanel />)
    await waitFor(() => expect(screen.getAllByTestId('memory-row')).toHaveLength(2))

    fireEvent.click(screen.getAllByTestId('memory-delete')[0])
    // 二次确认在场，且**还没**发 DELETE
    expect(screen.getByTestId('memory-delete-confirm')).toBeTruthy()
    expect(calls.find((c) => c.method === 'DELETE')).toBeUndefined()

    fireEvent.click(screen.getByTestId('memory-delete-confirm'))
    await waitFor(() => {
      const del = calls.find((c) => c.method === 'DELETE')
      expect(del).toBeTruthy()
      expect(del?.url).toContain('/api/copilot/memory/1')
    })
    await waitFor(() => expect(screen.getAllByTestId('memory-row')).toHaveLength(1))
  })

  it('删除：取消确认则不发 DELETE', async () => {
    render(<MemoryPanel />)
    await waitFor(() => expect(screen.getAllByTestId('memory-row')).toHaveLength(2))

    fireEvent.click(screen.getAllByTestId('memory-delete')[0])
    fireEvent.click(screen.getByTestId('memory-delete-cancel'))

    expect(calls.find((c) => c.method === 'DELETE')).toBeUndefined()
    expect(screen.getAllByTestId('memory-row')).toHaveLength(2)
  })

  it('编辑：双击内容变输入框，Enter 保存', async () => {
    render(<MemoryPanel />)
    await waitFor(() => expect(screen.getAllByTestId('memory-row')).toHaveLength(2))

    fireEvent.doubleClick(screen.getAllByTestId('memory-content')[0])
    const input = await screen.findByTestId('memory-edit-input')
    expect((input as HTMLInputElement).value).toBe('我只做主板，不碰北交所')

    fireEvent.change(input, { target: { value: '我只做主板和创业板' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    await waitFor(() => {
      const patch = calls.find((c) => c.method === 'PATCH')
      expect(patch).toBeTruthy()
      expect(JSON.parse(String(patch?.body)).content).toBe('我只做主板和创业板')
    })
    await waitFor(() => expect(screen.getByText('我只做主板和创业板')).toBeTruthy())
    // Enter 关掉输入框会触发一次 blur —— 不许因此多发一发 PATCH。
    await waitFor(() => {
      const patches = calls.filter((c) => c.method === 'PATCH')
      expect(patches).toHaveLength(1)
    })
  })

  it('编辑：Esc 取消，不发 PATCH', async () => {
    render(<MemoryPanel />)
    await waitFor(() => expect(screen.getAllByTestId('memory-row')).toHaveLength(2))

    fireEvent.doubleClick(screen.getAllByTestId('memory-content')[0])
    const input = await screen.findByTestId('memory-edit-input')
    fireEvent.change(input, { target: { value: '不该被保存' } })
    fireEvent.keyDown(input, { key: 'Escape' })

    await waitFor(() => expect(screen.queryByTestId('memory-edit-input')).toBeNull())
    expect(calls.find((c) => c.method === 'PATCH')).toBeUndefined()
    expect(screen.getByText('我只做主板，不碰北交所')).toBeTruthy()
  })

  it('合规：不出现单点买卖指令类措辞', async () => {
    render(<MemoryPanel />)
    await waitFor(() => expect(screen.getAllByTestId('memory-row')).toHaveLength(2))
    const text = document.body.textContent || ''
    for (const w of ['建议买入', '建议卖出', '主力建仓', '主力出货']) {
      expect(text).not.toContain(w)
    }
  })
})
