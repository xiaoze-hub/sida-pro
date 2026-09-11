import { describe, expect, it } from 'vitest'
import {
  DASHBOARD_LAYOUT_KEY,
  DASHBOARD_MODULES,
  defaultLayout,
  isHidden,
  loadLayout,
  moveModule,
  normalizeLayout,
  orderIndex,
  resetLayout,
  saveLayout,
  toggleModule,
} from '../../src/lib/dashboard-layout'

function fakeStorage(init: Record<string, string> = {}) {
  const data = { ...init }
  return {
    getItem: (k: string) => (k in data ? data[k] : null),
    setItem: (k: string, v: string) => {
      data[k] = v
    },
    dump: () => data,
  }
}

describe('DASHBOARD_MODULES 定义', () => {
  it('12 个模块且 id 唯一', () => {
    expect(DASHBOARD_MODULES.length).toBe(12)
    expect(new Set(DASHBOARD_MODULES.map((m) => m.id)).size).toBe(12)
  })
})

describe('toggleModule / isHidden', () => {
  it('隐藏→恢复往返, 不影响 order', () => {
    const l0 = defaultLayout()
    const l1 = toggleModule(l0, 'fundflow')
    expect(isHidden(l1, 'fundflow')).toBe(true)
    expect(l1.order).toEqual(l0.order)
    const l2 = toggleModule(l1, 'fundflow')
    expect(isHidden(l2, 'fundflow')).toBe(false)
  })
})

describe('moveModule 组内排序', () => {
  it('同组与相邻交换', () => {
    const l = moveModule(defaultLayout(), 'overview', -1) // main: [...,kpi,overview,...] → 与 kpi 换
    expect(orderIndex(l, 'overview')).toBe(1)
    expect(orderIndex(l, 'kpi')).toBe(2)
  })

  it('组内边界 no-op(上移首个/下移末个)', () => {
    const l0 = defaultLayout()
    expect(moveModule(l0, 'indices', -1).order).toEqual(l0.order)
    expect(moveModule(l0, 'discover', 1).order).toEqual(l0.order)
  })

  it('不跨组(main 末项下移是 no-op, 即使配置序下一个是 duo 的首个)', () => {
    const l0 = defaultLayout()
    const l1 = moveModule(l0, 'resonance', 1)
    expect(l1.order).toEqual(l0.order)
  })

  it('duo 组内交换只影响本组两员', () => {
    const l = moveModule(defaultLayout(), 'breadth', -1)
    expect(orderIndex(l, 'breadth')).toBe(0)
    expect(orderIndex(l, 'anomalies')).toBe(1)
  })

  it('workspace 内"工作台↔次级"同网格, 可互相上移(最新报告与机会精选换位)', () => {
    const l = moveModule(defaultLayout(), 'reports', -1)
    expect(orderIndex(l, 'reports')).toBe(2) // 原 picks 位
    expect(orderIndex(l, 'picks')).toBe(3)
  })
})

describe('normalizeLayout 容错', () => {
  it('未知 id 丢弃、重复去重、缺失补默认末尾', () => {
    const l = normalizeLayout({ order: ['picks', 'not-a-module', 'picks', 'indices'], hidden: ['zzz', 'kpi'] })
    expect(l.order.slice(0, 2)).toEqual(['picks', 'indices'])
    expect(l.order.length).toBe(12)
    expect(new Set(l.order).size).toBe(12)
    expect(l.hidden).toEqual(['kpi'])
  })

  it('垃圾输入回默认', () => {
    expect(normalizeLayout(null).order).toEqual(defaultLayout().order)
    expect(normalizeLayout('x').order).toEqual(defaultLayout().order)
    expect(normalizeLayout({ order: 'nope' }).order).toEqual(defaultLayout().order)
  })
})

describe('load/save 持久化', () => {
  it('roundtrip', () => {
    const st = fakeStorage()
    const l = toggleModule(moveModule(defaultLayout(), 'picks', -1), 'reports')
    saveLayout(l, st)
    expect(loadLayout(st)).toEqual(l)
  })

  it('损坏 JSON / 缺键 → 回默认不崩', () => {
    expect(loadLayout(fakeStorage({ [DASHBOARD_LAYOUT_KEY]: '{bad json' }))).toEqual(defaultLayout())
    expect(loadLayout(fakeStorage())).toEqual(defaultLayout())
  })

  it('resetLayout 回默认', () => {
    expect(resetLayout()).toEqual(defaultLayout())
  })
})
