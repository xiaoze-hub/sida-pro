// 发版后懒加载 chunk 404 自愈(2026-10-10)回归钉。
//
// 用户报障: 发版频繁(一周 12+ 版本), 旧页面仍开着, 点开尚未加载过的路由 → 动态 import 的
// chunk 已被新部署删除 → 整块空白, 必须**手动刷新**才能恢复。
//
// 本测试钉住 `lazyWithRetry` 的决策内核(注入 storage / reload, 不依赖 jsdom 导航实现):
//   1. 只对"chunk 加载失败"触发; 普通运行时错误原样抛, 不刷新;
//   2. 首次 chunk 失败 → 置 sessionStorage 标记 + reload 一次, 且挂起等待刷新;
//   3. 刷新后仍失败(标记仍在) → 清标记 + 抛错(交 ErrorBoundary), 不无限刷新;
//   4. 加载成功 → 清标记(重新武装), 下次发版再失败仍能自愈一次。
import { describe, expect, it, vi } from 'vitest'
import {
  clearChunkReloadFlag,
  handleChunkLoadError,
  isChunkLoadError,
  lazyWithRetry,
  withChunkRetry,
  type ChunkRetryDeps,
} from '@/lib/lazy-with-retry'

/** 内存版 sessionStorage 替身(断言标记读写)。 */
function memStorage() {
  const m = new Map<string, string>()
  return {
    getItem: (k: string) => (m.has(k) ? (m.get(k) as string) : null),
    setItem: (k: string, v: string) => void m.set(k, v),
    removeItem: (k: string) => void m.delete(k),
    /** 测试用: 当前是否已置标记 */
    has: (k: string) => m.has(k),
  }
}

/** 伪装成"旧页面加载已被删除的 chunk"。 */
function chunkError() {
  return new Error('Failed to fetch dynamically imported module: /assets/Dashboard-abc123.js')
}

const KEY = 'dashboard-page'

describe('isChunkLoadError 判定', () => {
  it('认得常见"动态 import 失败"措辞', () => {
    expect(isChunkLoadError(chunkError())).toBe(true)
    expect(isChunkLoadError(new Error('Loading chunk 42 failed.'))).toBe(true)
    expect(isChunkLoadError(new Error('Importing a module script failed.'))).toBe(true)
  })

  it('普通运行时错误不认(不能被当成 chunk 失败去刷新)', () => {
    expect(isChunkLoadError(new Error('Cannot read properties of undefined'))).toBe(false)
    expect(isChunkLoadError('some string')).toBe(false)
    expect(isChunkLoadError(null)).toBe(false)
  })
})

describe('handleChunkLoadError 决策', () => {
  it('非 chunk 错误 → pass, 不刷新', () => {
    const store = memStorage()
    const reload = vi.fn()
    expect(handleChunkLoadError(new Error('boom'), KEY, { storage: store, reload })).toBe('pass')
    expect(reload).not.toHaveBeenCalled()
    expect(store.has(`sida:chunk-reload:${KEY}`)).toBe(false)
  })

  it('首次 chunk 失败 → reloaded: 置标记 + 刷新一次', () => {
    const store = memStorage()
    const reload = vi.fn()
    expect(handleChunkLoadError(chunkError(), KEY, { storage: store, reload })).toBe('reloaded')
    expect(reload).toHaveBeenCalledTimes(1)
    expect(store.has(`sida:chunk-reload:${KEY}`)).toBe(true)
  })

  it('刷新后仍失败(标记仍在) → rethrow: 不再刷新, 且清标记', () => {
    const store = memStorage()
    const reload = vi.fn()
    handleChunkLoadError(chunkError(), KEY, { storage: store, reload }) // 首次: 置标记 + reload
    expect(reload).toHaveBeenCalledTimes(1)

    expect(handleChunkLoadError(chunkError(), KEY, { storage: store, reload })).toBe('rethrow')
    expect(reload).toHaveBeenCalledTimes(1) // 没有第二次刷新(防刷新环)
    expect(store.has(`sida:chunk-reload:${KEY}`)).toBe(false) // 标记已清
  })
})

describe('withChunkRetry 封装的工厂', () => {
  it('首次 chunk 失败 → 触发 reload 一次, 且返回的 promise 挂起(等待刷新, 不闪错误页)', async () => {
    const store = memStorage()
    const reload = vi.fn()
    const deps: ChunkRetryDeps = { storage: store, reload }
    const factory = withChunkRetry(() => Promise.reject(chunkError()), KEY, deps)

    const p = factory()
    // reject 回调在微任务里执行(reload 是异步触发的)
    await Promise.resolve()
    await Promise.resolve()
    expect(reload).toHaveBeenCalledTimes(1)

    let settled = false
    void p.then(
      () => { settled = true },
      () => { settled = true },
    )
    await Promise.resolve()
    await Promise.resolve()
    expect(settled).toBe(false) // 挂起: 依赖整页刷新

    // 刷新后同会话再触发(标记仍在) → 抛错交 ErrorBoundary, 不二次刷新
    const p2 = factory()
    await expect(p2).rejects.toThrow(/dynamically imported module/)
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('加载成功 → 清标记(重新武装), 供下次发版再自愈一次', async () => {
    const store = memStorage()
    const reload = vi.fn()
    store.setItem(`sida:chunk-reload:${KEY}`, '1') // 假设上一次自愈留下的标记
    const factory = withChunkRetry(() => Promise.resolve({ default: 'X' }), KEY, { storage: store, reload })

    await expect(factory()).resolves.toEqual({ default: 'X' })
    expect(store.has(`sida:chunk-reload:${KEY}`)).toBe(false)
  })

  it('普通运行时错误原样抛, 不刷新', async () => {
    const store = memStorage()
    const reload = vi.fn()
    const factory = withChunkRetry(() => Promise.reject(new Error('real crash')), KEY, { storage: store, reload })
    await expect(factory()).rejects.toThrow('real crash')
    expect(reload).not.toHaveBeenCalled()
  })
})

describe('lazyWithRetry 导出契约', () => {
  it('返回可被 React.lazy 消费的惰性组件(未读取前不触发工厂)', () => {
    const factory = vi.fn(() => Promise.resolve({ default: () => null }))
    const C = lazyWithRetry(factory as unknown as () => Promise<{ default: () => null }>, KEY, {
      storage: memStorage(),
      reload: vi.fn(),
    })
    expect(C).toBeTruthy()
    expect(factory).not.toHaveBeenCalled() // React.lazy 惰性: 首次渲染才读
    expect(typeof C.$$typeof).toBe('symbol')
  })

  it('clearChunkReloadFlag 可显式清标记', () => {
    const store = memStorage()
    store.setItem(`sida:chunk-reload:${KEY}`, '1')
    clearChunkReloadFlag(KEY, { storage: store })
    expect(store.has(`sida:chunk-reload:${KEY}`)).toBe(false)
  })
})
