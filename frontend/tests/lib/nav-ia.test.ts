/**
 * P2-2 钉子: 一级导航信息架构(设计稿 v3.0 §六: 一级 ≤6 + 新增「决策」入口)。
 *
 * 这条测试的由来是一个**真 bug**: 我加了 `/decision-ledger` 到 navItems, 但没加进任何分组的
 * filter 列表 —— 桌面侧栏里根本看不到它(只有 URL 和移动端"更多"能到)。
 * 所以除了"组数 ≤6", 还要钉**"navItems 里的项必须被某个分组接住"**, 防同类遗漏。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const src = readFileSync(resolve(__dirname, '../..', 'src/App.tsx'), 'utf-8')

/** 取 desktopNavGroups 里所有分组 filter 的 to 白名单 */
function groupedPaths(): string[] {
  const block = src.slice(src.indexOf('const desktopNavGroups'), src.indexOf('const MOBILE_PRIMARY_TO'))
  const lists = [...block.matchAll(/\[([^\]]*)\]\.includes\(n\.to\)/g)].map((m) => m[1])
  const out: string[] = []
  for (const l of lists) {
    for (const m of l.matchAll(/'([^']+)'/g)) out.push(m[1])
  }
  // 第一个分组是 `n.to === '/'` 形式(首页), 单独补
  if (/n\.to === '\/'/.test(block) || /'\/'/.test(block)) out.push('/')
  return out
}

/** 取 navItems 定义里的 to */
function navItemPaths(): string[] {
  const block = src.slice(src.indexOf('const navItems'), src.indexOf('const desktopNavGroups'))
  return [...block.matchAll(/to:\s*'([^']+)'/g)].map((m) => m[1])
}

/**
 * 明确不进桌面分组的项 —— **必须写清理由**(不是"懒得接")。
 * §4.3 已把这三页并入别处: 历史→报告(?tab=history)、模拟盘→影子(?tab=paper)、提醒→通知(?tab=alerts);
 * 它们的 navItems 条目保留只为**移动端"更多"**与**老书签跳转**, 桌面侧栏不再单独占位。
 */
const NOT_GROUPED = new Set<string>(['/history', '/paper-trading', '/alerts'])

describe('一级导航 ≤6 组', () => {
  it('分组数 ≤6', () => {
    const block = src.slice(src.indexOf('const desktopNavGroups'), src.indexOf('const MOBILE_PRIMARY_TO'))
    const n = [...block.matchAll(/\{\s*key:\s*'([a-z]+)'/g)].length
    expect(n).toBeGreaterThan(0)
    expect(n).toBeLessThanOrEqual(6)
  })

  it('有「决策」组, 且账本与口径对照都在里面(修掉"加了导航却看不到"的 bug)', () => {
    const grouped = groupedPaths()
    expect(src).toContain("key: 'decision'")
    expect(grouped).toContain('/decision-ledger')
    expect(grouped).toContain('/caliber-compare')
  })

  it('首页并入行情组(不再单独占一组)', () => {
    expect(src).not.toMatch(/key: 'cockpit'/)
    expect(groupedPaths()).toContain('/')
  })
})

describe('没有"孤儿"导航项', () => {
  it('navItems 里的每一项都被某个分组接住(否则桌面侧栏看不到)', () => {
    const grouped = new Set(groupedPaths())
    const orphans = navItemPaths().filter((p) => !grouped.has(p) && !NOT_GROUPED.has(p))
    expect(orphans).toEqual([])
  })
})
