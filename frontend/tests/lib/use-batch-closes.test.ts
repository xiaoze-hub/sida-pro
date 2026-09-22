// @vitest-environment jsdom
/**
 * 批量收盘价 hook(2026-09-22)。
 * 判据: ① 空代码集**不发请求**; ② 超过 60 只**自动分批**(后端有上限, 不打无谓的 400);
 * ③ 后端 missing 如实透出, **不补 0**; ④ 请求失败只影响走势图那一列(不抛到页面)。
 */
import { renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const fetchAPIMock = vi.fn()
vi.mock('@panwatch/api', () => ({ fetchAPI: (...a: unknown[]) => fetchAPIMock(...a) }))

import { useBatchCloses } from '@/hooks/useBatchCloses'

beforeEach(() => {
  fetchAPIMock.mockReset()
})

describe('useBatchCloses', () => {
  it('空代码集不发请求', () => {
    renderHook(() => useBatchCloses([]))
    expect(fetchAPIMock).not.toHaveBeenCalled()
  })

  it('正常回填: 逐标的收盘价进表, missing 如实透出', async () => {
    fetchAPIMock.mockResolvedValue({
      items: [{ symbol: '002361', closes: [1, 2, 3], source: 'tq' }],
      missing: ['600519'],
    })
    const { result } = renderHook(() => useBatchCloses(['002361', '600519']))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.closes['002361']).toEqual([1, 2, 3])
    expect(result.current.closes['600519']).toBeUndefined() // 不许补 0 / 空数组冒充
    expect(result.current.missing).toEqual(['600519'])
    // 口径可见: 来源如实透出(tq/tencent…), 缺来源就不编
    expect(result.current.sources['002361']).toBe('tq')
    expect(result.current.sources['600519']).toBeUndefined()
  })

  it('超过 60 只自动分批(两批请求), 结果合并', async () => {
    fetchAPIMock.mockImplementation((url: string) => {
      const syms = decodeURIComponent(String(url).match(/symbols=([^&]*)/)![1]).split(',')
      return Promise.resolve({ items: syms.map((s) => ({ symbol: s, closes: [1, 2] })), missing: [] })
    })
    const list = Array.from({ length: 65 }, (_, i) => `s${i}`)
    const { result } = renderHook(() => useBatchCloses(list))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(fetchAPIMock).toHaveBeenCalledTimes(2)
    expect(Object.keys(result.current.closes)).toHaveLength(65)
  })

  it('请求失败 → 该批全进 missing, 不抛错、不阻塞', async () => {
    fetchAPIMock.mockRejectedValue(new Error('boom'))
    const { result } = renderHook(() => useBatchCloses(['002361']))
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.missing).toEqual(['002361'])
    expect(result.current.closes).toEqual({})
  })

  it('代码集不变则不重复请求(数组字面量每次都是新引用, 靠 key 去抖)', async () => {
    fetchAPIMock.mockResolvedValue({ items: [], missing: [] })
    const { rerender } = renderHook(({ syms }) => useBatchCloses(syms), {
      initialProps: { syms: ['a', 'b'] },
    })
    await waitFor(() => expect(fetchAPIMock).toHaveBeenCalledTimes(1))
    rerender({ syms: ['b', 'a'] }) // 顺序变了, 集合没变
    await new Promise((r) => setTimeout(r, 30))
    expect(fetchAPIMock).toHaveBeenCalledTimes(1)
  })
})
