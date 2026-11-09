/**
 * 板块查找纯逻辑 (2026-11-09):
 * 概念板块约 457 条, 色块热力图里靠肉眼定位单个题材(如「商业航天」)不现实。
 *
 * 这里只做**本地、确定性**的过滤: 输入来自后端 `/boards/heatmap` 已返回的 items,
 * 按名称/代码包含匹配。不预置主题、不补条目、不改写涨幅 —— 找不到就是空结果,
 * 由 UI 明说「未找到」, 绝不用热门板块/默认排名填充。
 *
 * 排序仅按**匹配质量**(精确名 > 精确代码 > 前缀 > 包含), 同档保持后端原始顺序,
 * 因此不存在"我们自己算出来的热度排名"。
 */

import type { BoardHeatItem } from './board-heatmap'

export type BoardSearchMatch = 'exact-name' | 'exact-code' | 'prefix' | 'includes'

export interface BoardSearchHit {
  blockCode: string
  name: string
  /** 原样透传(可能为 null = 无数据), 不补 0 */
  changePct: number | null
  /** 原样透传(是否有当日数据) */
  hasDaily: boolean
  match: BoardSearchMatch
}

export interface BoardSearchResult {
  /** 实际返回的条目(受 limit 截断) */
  hits: BoardSearchHit[]
  /** 真实匹配总数(**不**是 hits.length) —— 展示上限不能当总数报给用户 */
  total: number
  /** 是否因 limit 截断(total > hits.length) */
  truncated: boolean
}

/** 查询归一化: 去首尾空白 + 小写(名称里中英文混排时英文忽略大小写) */
export function normalizeBoardQuery(raw: string): string {
  return (raw ?? '').trim().toLowerCase()
}

const MATCH_RANK: Record<BoardSearchMatch, number> = {
  'exact-name': 0,
  'exact-code': 1,
  prefix: 2,
  includes: 3,
}

function classify(nameLower: string, codeLower: string, q: string): BoardSearchMatch | null {
  if (nameLower === q) return 'exact-name'
  if (codeLower === q) return 'exact-code'
  if (nameLower.startsWith(q) || codeLower.startsWith(q)) return 'prefix'
  if (nameLower.includes(q) || codeLower.includes(q)) return 'includes'
  return null
}

/**
 * 在已返回板块里查找, 同时给出**真实匹配总数**。
 *
 * 为什么要 total: 面板只展示前 12 条(不刷屏), 但曾把 `hits.length` 当总数显示成
 * 「12 个匹配」—— 其实命中 30 个。总数与展示上限必须分开报。
 *
 * @param items 后端返回的板块列表(不额外请求、不跨视图猜数据)
 * @param rawQuery 用户输入; 空/全空白 → 空结果(不倾倒全量板块)
 * @param opts.limit 最多返回条数(默认 12, UI 小面板不刷屏), 仅截断不改匹配语义
 */
export function searchBoardMatches(
  items: readonly BoardHeatItem[],
  rawQuery: string,
  opts: { limit?: number } = {},
): BoardSearchResult {
  const q = normalizeBoardQuery(rawQuery)
  if (!q) return { hits: [], total: 0, truncated: false }
  const limit = Math.max(0, opts.limit ?? 12)
  const hits: Array<BoardSearchHit & { rank: number }> = []
  for (const it of items) {
    const name = it.name ?? ''
    const code = it.block_code ?? ''
    const match = classify(name.toLowerCase(), code.toLowerCase(), q)
    if (!match) continue
    hits.push({
      blockCode: code,
      name: name || code,
      changePct: it.change_pct,
      hasDaily: Boolean(it.has_daily),
      match,
      rank: MATCH_RANK[match],
    })
  }
  // Array.prototype.sort 是稳定排序 ⇒ 同档保持后端顺序。
  hits.sort((a, b) => a.rank - b.rank)
  const limited = hits.slice(0, limit).map(({ rank: _rank, ...hit }) => hit)
  return { hits: limited, total: hits.length, truncated: hits.length > limited.length }
}

/** 只要条目(兼容旧调用方); 需要总数请用 {@link searchBoardMatches}。 */
export function searchBoards(
  items: readonly BoardHeatItem[],
  rawQuery: string,
  opts: { limit?: number } = {},
): BoardSearchHit[] {
  return searchBoardMatches(items, rawQuery, opts).hits
}
