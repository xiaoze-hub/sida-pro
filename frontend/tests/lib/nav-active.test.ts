import { describe, it, expect } from 'vitest'
import { isNavItemActive } from '@/lib/nav-active'

// 2026-09-13: 「行情」项 to 带 query(/stocks/000001?type=index), pathname 不含 query,
// 旧 `pathname.startsWith(to)` 恒 false → 永不选中。以下用例锁定修复后的规则。
describe('nav-active / isNavItemActive', () => {
  it('to 带 query 的「行情」项在任意 /stocks/:symbol 下都选中(含默认 000001)', () => {
    const to = '/stocks/000001?type=index'
    expect(isNavItemActive(to, '/stocks/000001')).toBe(true) // 默认落点
    expect(isNavItemActive(to, '/stocks/002636')).toBe(true) // 其它 symbol 也属同一工作台
    expect(isNavItemActive(to, '/stocks/600519')).toBe(true)
    expect(isNavItemActive(to, '/stocks/880001')).toBe(true)
  })

  it('to 带 query 的「行情」项不误命中 /stocks 之外的路径', () => {
    const to = '/stocks/000001?type=index'
    expect(isNavItemActive(to, '/')).toBe(false)
    expect(isNavItemActive(to, '/heatmap')).toBe(false)
    expect(isNavItemActive(to, '/stocks-archive')).toBe(false) // 前缀形近但不在 /stocks/ 下
  })

  it('无 query 的项保持原前缀语义不变', () => {
    expect(isNavItemActive('/heatmap', '/heatmap')).toBe(true)
    expect(isNavItemActive('/heatmap', '/theme-mood')).toBe(false)
    expect(isNavItemActive('/portfolio', '/portfolio')).toBe(true)
    expect(isNavItemActive('/portfolio', '/portfolio/123')).toBe(true) // 原行为: 子路径也选中
  })

  it('首页项仅在精确 / 时选中(原特殊处理不变)', () => {
    expect(isNavItemActive('/', '/')).toBe(true)
    expect(isNavItemActive('/', '/heatmap')).toBe(false)
  })

  it('既带 query 又非 /stocks 的项按 pathname 部分前缀比对', () => {
    expect(isNavItemActive('/notifications?tab=alerts', '/notifications')).toBe(true)
    expect(isNavItemActive('/notifications?tab=alerts', '/heatmap')).toBe(false)
  })
})
