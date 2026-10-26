import { lazy, type ComponentType, type LazyExoticComponent } from 'react'

/**
 * 发版后懒加载 chunk 404 自愈(2026-10-10)。
 *
 * 背景(用户报障): 发版频繁(一周 12+ 个版本), 旧页面仍开着, 此时点开一个尚未加载过的
 * 路由 → 动态 import 的 chunk 已被新部署删除 → 浏览器 404/Hash 失配 → React.lazy 抛错 →
 * 整块空白, 必须**手动刷新**才能恢复。
 *
 * 方案: 把 `() => import(...)` 封一层 —— 只在**chunk 加载失败**时自动 `location.reload()`
 * 一次, 用 `sessionStorage` 标记防刷新环:
 *   - 首次失败: 置标记 → reload(刷新后拿到新 manifest/新 chunk);
 *   - 刷新后仍失败: 标记已在 → 清标记并原样抛出 → 交给外层 ErrorBoundary 出错误页(不再刷);
 *   - 加载成功: 清标记(重新武装), 下次发版换 chunk 再失败仍能自愈一次。
 *
 * 只对 chunk 加载类错误触发(见 `isChunkLoadError`); 普通 JS 运行时错误原样抛, 不触发刷新
 * (否则代码 bug 会变成无限刷新/掩盖问题)。
 *
 * `key` 必须是**跨 reload 稳定**的静态字符串(如页面名)—— 它进 sessionStorage 标记,
 * 动态生成的 key 在刷新后会变, 标记对不上就会无限刷新。
 */

const FLAG_PREFIX = 'sida:chunk-reload:'

/** 判定"动态 import 的 chunk 加载失败"(各浏览器措辞不同, 覆盖常见几种)。 */
export function isChunkLoadError(err: unknown): boolean {
  const msg = String((err as { message?: unknown })?.message ?? err ?? '')
  return /dynamically imported module|Loading chunk|Loading CSS chunk|Importing a module script failed|error loading dynamically imported module/i.test(
    msg,
  )
}

type StorageLike = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

export interface ChunkRetryDeps {
  /** 注入存储(测试用); 显式传 `null` = 无存储 */
  storage?: StorageLike | null
  /** 注入刷新函数(测试用) */
  reload?: () => void
}

function resolveStorage(deps?: ChunkRetryDeps): StorageLike | null {
  if (deps && 'storage' in deps) return deps.storage ?? null
  try {
    if (typeof window !== 'undefined' && window.sessionStorage) return window.sessionStorage
  } catch {
    /* 隐私模式/被禁用 */
  }
  return null
}

function defaultReload(): void {
  try {
    window.location.reload()
  } catch {
    /* 无法刷新时保持现状(由调用方决定后续) */
  }
}

export type ChunkErrorOutcome = 'pass' | 'reloaded' | 'rethrow'

/**
 * 纯决策函数: 拿到懒加载错误后该做什么。
 * - `pass`:    非 chunk 错误 → 不处理, 原样抛(不刷新);
 * - `reloaded`: 首次 chunk 失败 → 置标记 + reload(应挂起等待刷新);
 * - `rethrow`: 本会话已自动刷新过一次仍失败 → 清标记 + 抛(交 ErrorBoundary, 防刷新环)。
 */
export function handleChunkLoadError(err: unknown, key: string, deps?: ChunkRetryDeps): ChunkErrorOutcome {
  if (!isChunkLoadError(err)) return 'pass'
  const storage = resolveStorage(deps)
  const reload = deps?.reload ?? defaultReload
  const flag = FLAG_PREFIX + key
  if (storage?.getItem(flag)) {
    try {
      storage.removeItem(flag)
    } catch {
      /* ignore */
    }
    return 'rethrow'
  }
  try {
    storage?.setItem(flag, '1')
  } catch {
    /* ignore */
  }
  reload()
  return 'reloaded'
}

/** 清掉某 key 的"已自动刷新"标记(加载成功时调用, 让自愈机制重新武装)。 */
export function clearChunkReloadFlag(key: string, deps?: ChunkRetryDeps): void {
  const storage = resolveStorage(deps)
  try {
    storage?.removeItem(FLAG_PREFIX + key)
  } catch {
    /* ignore */
  }
}

/**
 * 把任意 `() => import(...)` 工厂包成"chunk 失败自愈"的工厂(React.lazy 直接吃它)。
 * 独立导出便于单测(不依赖 jsdom 的导航实现)。
 */
export function withChunkRetry<T>(
  factory: () => Promise<T>,
  key: string,
  deps?: ChunkRetryDeps,
): () => Promise<T> {
  return () =>
    factory().then(
      (mod) => {
        // 加载成功 → 清标记(重新武装)
        clearChunkReloadFlag(key, deps)
        return mod
      },
      (err) => {
        const outcome = handleChunkLoadError(err, key, deps)
        if (outcome === 'reloaded') {
          // 刷新在途: 返回永不 resolve 的 promise —— 让 Suspense 停在 fallback,
          // 避免刷新前闪一下错误页(浏览器随即整页重载)。
          return new Promise<T>(() => {})
        }
        throw err
      },
    )
}

/**
 * React.lazy 的韧性封装: 覆盖所有路由级懒加载。
 * @param factory `() => import('...')`
 * @param key     跨 reload 稳定的唯一键(如页面名)
 */
export function lazyWithRetry<T extends ComponentType<any>>(
  factory: () => Promise<{ default: T }>,
  key: string,
  deps?: ChunkRetryDeps,
): LazyExoticComponent<T> {
  return lazy(withChunkRetry(factory, key, deps))
}

export default lazyWithRetry
