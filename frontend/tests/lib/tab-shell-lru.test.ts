/**
 * L2-E: 页签上限 8 + LRU 淘汰 —— 2026-10-24。
 *
 * 交接文档（docs/design/20261024_剩余4项交接_Hermes.md）第 4 项。
 * 上一版是 **FIFO**（`upsertTab` 里 `findIndex((tab,i) => i>0 && tab.key !== spec.key)` = 最早打开的非首页）。
 * 问题：用户在 A/B 之间反复切，A 虽然一直没用但"最早打开"，先被挤掉 —— 用户会觉得莫名其妙丢页。
 *
 * 本次改成 **LRU**：访问顺序记录在壳层 `useRef`，`pickEvictionTarget` 只算"淘汰谁"，
 * **不动页签数组顺序**（数组顺序 = 打开顺序，视觉稳定；也是 workspace-tab-shell.test.tsx
 * 里成片 `expect(labels()).toEqual([...])` 能继续绿的前提）。
 */
import { describe, expect, it } from 'vitest'

import {
  HOME_TAB_KEY,
  MAX_TABS,
  closeOtherTabs,
  closeTabsToRight,
  normalizeTabs,
  pickEvictionTarget,
  upsertTab,
  type WorkspaceTab,
} from '@/lib/tab-shell'

const tab = (key: string): WorkspaceTab => ({
  key,
  path: key,
  closable: key !== HOME_TAB_KEY,
})
const keys = (tabs: WorkspaceTab[]) => tabs.map((t) => t.key)

describe('L2-E: 上限 8', () => {
  it('上限是 8（FIFO 时代是 12）', () => {
    expect(MAX_TABS).toBe(8)
  })

  it('超过 8 个只留 8 个，首页恒在首位', () => {
    let tabs = normalizeTabs([])
    for (let i = 0; i < MAX_TABS + 3; i++) {
      tabs = upsertTab(tabs, { key: `/p${i}`, path: `/p${i}` })
    }
    expect(tabs.length).toBe(MAX_TABS)
    expect(tabs[0].key).toBe(HOME_TAB_KEY)
  })
})

describe('L2-E: pickEvictionTarget（只选淘汰目标，不重排）', () => {
  const three = () => normalizeTabs([tab('/a'), tab('/b'), tab('/c')])

  it('取最久未访问的非首页页签', () => {
    // touch 顺序：/a 最旧 → /c 最新
    expect(pickEvictionTarget(three(), ['/a', '/b', '/c'], '/d')).toBe('/a')
    // 用户刚回到 /a，最久没碰的变成 /c
    expect(pickEvictionTarget(three(), ['/c', '/b', '/a'], '/d')).toBe('/c')
  })

  it('跳过首页、跳过正要打开的那个', () => {
    expect(pickEvictionTarget(three(), [HOME_TAB_KEY, '/a', '/b'], '/a')).toBe('/b')
  })

  it('LRU 记录里的死 key 跳过，退回落 tabs 顺序的第一个非首页', () => {
    // /gone 早已不存在 —— 不能因为它在记录里就返回一个不存在的 key
    expect(pickEvictionTarget(three(), ['/gone'], '/z')).toBe('/a')
  })

  it('只剩首页时没有可淘汰目标', () => {
    expect(pickEvictionTarget(normalizeTabs([]), [], '/a')).toBeUndefined()
  })
})

describe('L2-E: upsertTab 按 evictKey 淘汰', () => {
  const fill = () => {
    let tabs = normalizeTabs([])
    for (const key of ['/a', '/b', '/c']) {
      tabs = upsertTab(tabs, { key, path: key }, 4)
    }
    return tabs
  }

  it('传 evictKey：淘汰指定的 LRU 目标，其余顺序保持打开顺序', () => {
    const next = upsertTab(fill(), { key: '/d', path: '/d' }, 4, '/b')
    expect(keys(next)).toEqual([HOME_TAB_KEY, '/a', '/c', '/d'])
  })

  it('不传 evictKey：保持旧行为（丢最早打开的非首页）', () => {
    const next = upsertTab(fill(), { key: '/d', path: '/d' }, 4)
    expect(keys(next)).toEqual([HOME_TAB_KEY, '/b', '/c', '/d'])
  })

  it('切换已有页签不触发淘汰（同 key 不重排、不增不减）', () => {
    const base = fill()
    const next = upsertTab(base, { key: '/b', path: '/b' }, 4, '/a')
    expect(keys(next)).toEqual(keys(base))
  })
})

describe('L2-E: 批量关闭', () => {
  const four = () => normalizeTabs([tab('/a'), tab('/b'), tab('/c'), tab('/d')])

  it('关闭其他：只留首页 + 锚点', () => {
    expect(keys(closeOtherTabs(four(), '/b'))).toEqual([HOME_TAB_KEY, '/b'])
  })

  it('关闭其他：锚点是首页时只剩首页', () => {
    expect(keys(closeOtherTabs(four(), HOME_TAB_KEY))).toEqual([HOME_TAB_KEY])
  })

  it('关闭右侧：保留锚点及其左侧', () => {
    expect(keys(closeTabsToRight(four(), '/b'))).toEqual([HOME_TAB_KEY, '/a', '/b'])
  })

  it('关闭右侧：锚点在最后时无变化', () => {
    expect(keys(closeTabsToRight(four(), '/d'))).toEqual([HOME_TAB_KEY, '/a', '/b', '/c', '/d'])
  })

  it('锚点不存在时原样返回（不误删）', () => {
    expect(keys(closeOtherTabs(four(), '/nope'))).toEqual([HOME_TAB_KEY, '/a', '/b', '/c', '/d'])
    expect(keys(closeTabsToRight(four(), '/nope'))).toEqual([HOME_TAB_KEY, '/a', '/b', '/c', '/d'])
  })
})
