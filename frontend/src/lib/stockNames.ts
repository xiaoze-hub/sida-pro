/**
 * 股票名称解析(2026-10-21) —— 统一「名称 + 代码」展示的基础层。
 *
 * 问题: 不少页面只显示 6 位代码(决策账本明细 / 聚宝盆 / 策略应用结果 / 裁判战绩 …),
 * 用户看不出是哪只票。后端为此新开轻量端点:
 *
 *   GET /api/stocks/names?symbols=600111,000001&market=CN
 *   → {"code":0,"success":true,"data":{"names":{"600111":"北方稀土"},"missing":["000001"]}}
 *
 * 三条纪律:
 *  ① **拿不到就是没有**: 端点未上线/失败/该码缺失 ⇒ 缓存里没有这个键, 展示层自己回退成只显示代码,
 *     绝不编造「未知」「--」这类假名称;
 *  ② **失败静默**: 名称是锦上添花, 不能因为查不到名字弹错、抛错、阻塞列表;
 *  ③ **不打爆后端**: 模块级缓存 + 24h localStorage 持久化 + 每批 80 只上限 + 串行请求,
 *     同一代码一次会话内只问一次(失败的那批会放回\"未请求\", 下次挂载可重试)。
 *
 * 纯 TS 模块(无 React), 供组件/hook 与纯逻辑测试共用。
 */
import { fetchAPI } from '@panwatch/api'

/** localStorage 键(带版本号: 结构变了直接换键, 不做迁移)。 */
const STORAGE_KEY = 'pw.stockNames.v1'
/** 持久化有效期: 24h(公司改名极少, 一天一次足够)。 */
const TTL_MS = 24 * 60 * 60 * 1000
/** 单次请求上限(后端一次最多吃这么多; 超了先切好, 不打无谓的 400)。 */
const MAX_PER_BATCH = 80

interface StoredPayload {
  /** 写入时刻(ms) */
  savedAt: number
  /** code → 名称 */
  names: Record<string, string>
}

/** 内存缓存: code → 名称。**命中 = 有名字**(没有键 = 不知道, 不是空名字)。 */
const cache = new Map<string, string>()
/** 已请求过的代码(成功或后端明确 missing)。避免重复打后端。 */
const requested = new Set<string>()
/** 缓存变更版本号 + 订阅者: 供 useStockNames 通过 useSyncExternalStore 感知。 */
let version = 0
const listeners = new Set<() => void>()

function notify(): void {
  version += 1
  for (const fn of Array.from(listeners)) {
    try {
      fn()
    } catch {
      // 订阅者自身异常不能传染给其它订阅者
    }
  }
}

/** 订阅缓存变更(返回退订函数)。 */
export function subscribeStockNames(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

/** 缓存版本号(useSyncExternalStore 的快照值; 数字天然稳定)。 */
export function getStockNamesVersion(): number {
  return version
}

let hydrated = false

/** 首次访问时把 localStorage 里的名称灌进内存缓存(过期/坏数据一律当没有)。 */
function hydrate(): void {
  if (hydrated) return
  hydrated = true
  try {
    const raw = globalThis.localStorage?.getItem(STORAGE_KEY)
    if (!raw) return
    const parsed = JSON.parse(raw) as Partial<StoredPayload> | null
    if (!parsed || typeof parsed !== 'object') return
    const savedAt = typeof parsed.savedAt === 'number' ? parsed.savedAt : 0
    if (!savedAt || Date.now() - savedAt > TTL_MS) return
    const names = parsed.names
    if (!names || typeof names !== 'object') return
    for (const [code, name] of Object.entries(names)) {
      if (typeof name === 'string' && name.trim()) {
        cache.set(code, name.trim())
        requested.add(code)
      }
    }
  } catch {
    // 读到坏 JSON / localStorage 被禁: 当作没缓存, 不影响页面
  }
}

/** 把内存缓存写回 localStorage(带写入时刻; 失败静默 —— 隐私模式下 setItem 会抛)。 */
function persist(): void {
  try {
    const names: Record<string, string> = {}
    for (const [code, name] of cache) names[code] = name
    const payload: StoredPayload = { savedAt: Date.now(), names }
    globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify(payload))
  } catch {
    // 存不下就算了: 内存缓存照样生效
  }
}

/** 代码归一(只去空白/大写, 不改写交易所前后缀 —— 索引/板块代码不能与个股混为一谈)。 */
function normCode(raw: string): string {
  return String(raw ?? '').trim().toUpperCase()
}

/**
 * 取已知名称; 没有则 `undefined`(**不给占位符**)。
 * 展示层据此决定「名称 + 代码」还是「只有代码」。
 */
export function getName(code: string): string | undefined {
  hydrate()
  const c = normCode(code)
  if (!c) return undefined
  return cache.get(c)
}

/**
 * 批量补名称(带缓存/去重/分批/串行)。**永不 reject**。
 *
 * @param codes 待查代码(重复/空值/已缓存/已请求过的会被过滤掉)
 * @returns 所有批次跑完(或全部命中缓存)后 resolve 的 Promise
 */
export function fetchNames(codes: string[]): Promise<void> {
  hydrate()
  const need: string[] = []
  const seen = new Set<string>()
  for (const raw of codes ?? []) {
    const c = normCode(raw)
    if (!c || cache.has(c) || requested.has(c) || seen.has(c)) continue
    seen.add(c)
    need.push(c)
  }
  if (need.length === 0) return Promise.resolve()

  // 先占位: 同一批代码在途时, 后到的调用方不会重复发请求
  for (const c of need) requested.add(c)

  const batches: string[][] = []
  for (let i = 0; i < need.length; i += MAX_PER_BATCH) batches.push(need.slice(i, i + MAX_PER_BATCH))

  void (async () => {
    for (const batch of batches) {
      try {
        const res = await fetchAPI<{
          names?: Record<string, string> | null
          missing?: string[] | null
        }>(`/stocks/names?symbols=${encodeURIComponent(batch.join(','))}&market=CN`)
        let changed = false
        const names = res?.names
        if (names && typeof names === 'object') {
          for (const code of batch) {
            const name = names[code]
            if (typeof name === 'string' && name.trim()) {
              cache.set(code, name.trim())
              changed = true
            }
          }
        }
        // 后端明确回了 missing 的: 保持\"已请求\", 本会话不再追问(它确实没有)
        if (changed) {
          persist()
          notify()
        }
      } catch {
        // 该批失败(端点未上线 / 网络抖动): 放回\"未请求\", 下次挂载可重试;
        // 展示层继续只显示代码 —— 不抛、不弹错、不阻塞列表。
        for (const code of batch) requested.delete(code)
      }
    }
  })()

  return Promise.resolve()
}

/** 清空全部名称缓存(登出 / 测试用)。 */
export function clearStockNamesCache(): void {
  cache.clear()
  requested.clear()
  hydrated = true
  try {
    globalThis.localStorage?.removeItem(STORAGE_KEY)
  } catch {
    // ignore
  }
  notify()
}
