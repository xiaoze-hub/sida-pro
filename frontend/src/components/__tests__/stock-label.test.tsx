// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * StockLabel 展示口径回归(2026-10-21)。
 *
 * 守三件事(用户反馈: 不少页面只给代码, 看不出是哪只票):
 *  ① 有名称 ⇒ 名称上屏 + 代码作为次要信息仍在(可复制/可核对);
 *  ② 没名称(名称端点未上线/该码缺失)⇒ **只显示代码**, 不出现「未知」「--」这类占位;
 *  ③ `showCode={false}` ⇒ 只显示名称(窄容器场景)。
 *
 * 网络一律 mock: 名称是锦上添花, 本用例同时证明\"拿不到名字不抛错\"。
 */

const api = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@sida/api', () => ({
  getToken: () => 'test-token',
  clearToken: () => {},
  fetchAPI: (...a: unknown[]) => api.fetchAPI(...a),
}))

import { StockLabel } from '@/components/StockLabel'
import { clearStockNamesCache } from '@/lib/stockNames'

beforeEach(() => {
  clearStockNamesCache()
  api.fetchAPI.mockReset()
  // 默认: 端点还没上线 —— 请求失败。所有用例的基线都是\"没有名字\"
  api.fetchAPI.mockRejectedValue(new Error('404 /api/stocks/names'))
})

afterEach(() => {
  cleanup()
  api.fetchAPI.mockReset()
})

describe('StockLabel 名称+代码', () => {
  it('有 name(prop)时显示名称, 且仍包含代码', () => {
    render(<StockLabel symbol="600111" name="北方稀土" />)
    const el = screen.getByTestId('stock-label')
    expect(el.textContent).toContain('北方稀土')
    expect(el.textContent).toContain('600111')
    expect(el.getAttribute('data-resolved')).toBe('true')
  })

  it('没有 name 且名称接口失败时只显示代码, 不渲染占位文案', async () => {
    render(<StockLabel symbol="000001" />)
    const el = screen.getByTestId('stock-label')
    expect(el.textContent).toBe('000001')
    expect(el.textContent).not.toContain('未知')
    expect(el.textContent).not.toContain('--')
    expect(el.getAttribute('data-resolved')).toBe('false')
    // 失败已经发生(端点 404)但仍只渲染代码 —— 静默回退
    await waitFor(() => expect(api.fetchAPI).toHaveBeenCalled())
    expect(screen.getByTestId('stock-label').textContent).toBe('000001')
  })

  it('showCode={false} 时只显示名称', () => {
    render(<StockLabel symbol="600111" name="北方稀土" showCode={false} />)
    const el = screen.getByTestId('stock-label')
    expect(el.textContent).toBe('北方稀土')
    expect(el.textContent).not.toContain('600111')
  })

  it('没有 name prop 时由名称接口补上(批量端点 → 名称上屏)', async () => {
    api.fetchAPI.mockResolvedValue({ names: { '600519': '贵州茅台' }, missing: [] })
    render(<StockLabel symbol="600519" />)
    await waitFor(() => {
      expect(screen.getByTestId('stock-label').textContent).toContain('贵州茅台')
    })
    const el = screen.getByTestId('stock-label')
    expect(el.textContent).toContain('600519')
    expect(api.fetchAPI.mock.calls[0][0]).toContain('/stocks/names?symbols=600519')
  })
})
