/**
 * P2-2 钉子: 一级导航信息架构(设计稿 v3.0 §六: 一级 ≤6 + 新增「决策」入口)。
 *
 * 这条测试的由来是一个**真 bug**: 曾经 `/decision-ledger` 进了 navItems, 但没加进任何分组的
 * filter 列表 —— 桌面侧栏里根本看不到它(只有 URL 和移动端"更多"能到)。
 * 所以除了"组数 ≤6", 还要钉**"导航项必须被某个分组接住"**, 防同类遗漏。
 *
 * 2026-11-07 P0: 导航清单从 App.tsx 挪到 lib/navigation.ts(单一事实源), 断言直接读数据表,
 * 不再 grep 源码 —— 结构改动不再需要同步改正则。
 */
import { describe, expect, it } from 'vitest'

import { NAV_GROUPS, NAV_ITEMS } from '@/lib/navigation'

describe('一级导航 ≤6 组', () => {
  it('分组数 ≤6', () => {
    expect(NAV_GROUPS.length).toBeGreaterThan(0)
    expect(NAV_GROUPS.length).toBeLessThanOrEqual(6)
  })

  it('有「决策」组, 且账本与口径对照都在里面(修掉"加了导航却看不到"的 bug)', () => {
    const decision = NAV_ITEMS.filter((n) => n.group === 'decision').map((n) => n.to)
    expect(NAV_GROUPS.some((g) => g.key === 'decision')).toBe(true)
    expect(decision).toContain('/decision-ledger')
    expect(decision).toContain('/caliber-compare')
  })

  it('首页并入行情组(不再单独占一组)', () => {
    expect(NAV_GROUPS.some((g) => (g.key as string) === 'cockpit')).toBe(false)
    const market = NAV_ITEMS.filter((n) => n.group === 'market').map((n) => n.to)
    expect(market).toContain('/')
  })
})

describe('没有"孤儿"导航项', () => {
  it('每个有分组意图的入口都归到已知分组', () => {
    const keys = new Set<string>(NAV_GROUPS.map((g) => g.key))
    const orphans = NAV_ITEMS.filter((n) => n.group !== null && !keys.has(n.group))
    expect(orphans.map((n) => n.to)).toEqual([])
  })

  it('每个分组至少有一个入口(桌面侧栏不出现空组)', () => {
    const empty = NAV_GROUPS.filter((g) => !NAV_ITEMS.some((n) => n.group === g.key))
    expect(empty.map((g) => g.key)).toEqual([])
  })

  it('明确不进分组的入口写清用途: 独立入口(数智参谋) + 已收纳的页内页签', () => {
    // group: null 是**产品决定**, 不是漏接: 数智参谋是独立高亮卡; 历史/模拟盘/提醒已并入
    // 报告/影子/通知(菜单里不再单列, 但移动端"更多"与命令面板仍可直达)。
    const ungrouped = NAV_ITEMS.filter((n) => n.group === null).map((n) => n.to)
    expect(new Set(ungrouped)).toEqual(
      new Set(['/ai-copilot', '/reports?tab=history', '/shadow?tab=paper', '/notifications?tab=alerts']),
    )
  })
})
