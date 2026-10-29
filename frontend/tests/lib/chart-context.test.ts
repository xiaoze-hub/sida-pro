/**
 * 图表上下文（P2, 2026-10-29）纯逻辑契约。
 *
 * 这一层是「个股工作台 → 数智参谋」唯一允许把图表状态变成**文字**的地方，所以它守的不是渲染，
 * 而是**可证明性**：
 *   ① 核心身份（账号 / 代码 / 标的类型 / 市场）任一拿不到 → 整个上下文作废（不猜）；
 *   ② 周期只认页面 URL（`?period=`），非法/缺失就不写 —— 不拿图表默认值冒充用户选择；
 *   ③ 可视区间只认图表真实上报的读数（`onRangeStats`），字段不全/非有限数/日期不合法 → 整段不写，
 *      不写一半；
 *   ④ 价位只认服务端已确认的标注行，负数/非有限/重复价一律丢；
 *   ⑤ 仓库同号歧义（000001 = 上证指数 + 平安银行）在读取侧**宁可返回 null 也不猜类型**。
 *
 * 另有一条纪律写进 prompt：只在用户主动提问涉及这张图时引用，且不得自动下单/自动画线。
 */
import { beforeEach, describe, expect, it } from 'vitest'

import {
  CHART_CONTEXT_MAX_LEVELS,
  buildChartContext,
  chartContextKey,
  chartContextParts,
  chartContextPrompt,
  chartContextStoreSize,
  clearChartContextStore,
  publishChartContext,
  readChartContext,
  unpublishChartContext,
  type ChartContext,
} from '../../src/lib/copilot/chart-context'

const OWNER = 'test-user'

/** 一份字段齐全的合法输入（用例只覆盖自己要破坏的那一项）。 */
function input(overrides: Record<string, unknown> = {}) {
  return {
    owner: OWNER,
    symbol: '600111',
    instrument: 'stock',
    market: 'CN',
    period: 'd1',
    range: { from: '2026-06-01', to: '2026-10-29', bars: 58, changePct: 12.34, amplitudePct: 23.45 },
    levels: [
      { price: 12.34, kind: 'support', label: '底仓成本' },
      { price: 15.2, kind: 'pressure', label: '压力' },
    ],
    now: 1_700_000_000_000,
    ...overrides,
  }
}

describe('buildChartContext · 身份不可证明就不带', () => {
  it('字段齐全 → 上下文完整（周期/区间/价位都在）', () => {
    const ctx = buildChartContext(input())
    expect(ctx).not.toBeNull()
    expect(ctx).toMatchObject({
      owner: OWNER,
      symbol: '600111',
      instrument: 'stock',
      market: 'CN',
      period: 'd1',
      capturedAt: 1_700_000_000_000,
    })
    expect(ctx?.range).toEqual({
      from: '2026-06-01',
      to: '2026-10-29',
      bars: 58,
      changePct: 12.34,
      amplitudePct: 23.45,
    })
    expect(ctx?.levels).toEqual([
      { price: 12.34, kind: 'support', label: '底仓成本' },
      { price: 15.2, kind: 'pressure', label: '压力' },
    ])
  })

  it.each([
    ['没有账号', { owner: null }],
    ['账号是空串', { owner: '   ' }],
    ['没有代码', { symbol: '' }],
    ['代码带非法字符', { symbol: '600 111' }],
    ['类型不可判定', { instrument: 'future' }],
    ['类型缺失', { instrument: null }],
    ['市场不可判定', { market: 'XX' }],
  ])('%s → null（整段作废，不传半份）', (_name, overrides) => {
    expect(buildChartContext(input(overrides as Record<string, unknown>))).toBeNull()
  })

  it('代码两侧空白会被规整（不是当作合法字符）', () => {
    expect(buildChartContext(input({ symbol: ' 600111 ' }))?.symbol).toBe('600111')
  })
})

describe('buildChartContext · 周期只认 URL', () => {
  it.each([['intra'], ['bogus'], [''], [null], [undefined]])('周期 %s → 不写 period', (period) => {
    const ctx = buildChartContext(input({ period }))
    expect(ctx).not.toBeNull()
    expect(ctx?.period).toBeUndefined()
  })

  it('分时不是 K 线周期 → 不冒充日线', () => {
    expect(buildChartContext(input({ period: 'intra' }))?.period).toBeUndefined()
  })

  it('周线/月线保留原值', () => {
    expect(buildChartContext(input({ period: 'w1' }))?.period).toBe('w1')
    expect(buildChartContext(input({ period: 'mn' }))?.period).toBe('mn')
  })
})

describe('buildChartContext · 可视区间无法证明就不写', () => {
  it('区间为 null → 不写 range，其余照旧', () => {
    const ctx = buildChartContext(input({ range: null }))
    expect(ctx).not.toBeNull()
    expect(ctx?.range).toBeUndefined()
  })

  it.each([
    ['缺 from', { to: '2026-10-29', bars: 3, changePct: 1, amplitudePct: 2 }],
    ['缺 to', { from: '2026-06-01', bars: 3, changePct: 1, amplitudePct: 2 }],
    ['日期不是 ISO', { from: '2026/06/01', to: '2026-10-29', bars: 3, changePct: 1, amplitudePct: 2 }],
    ['前后颠倒', { from: '2026-10-29', to: '2026-06-01', bars: 3, changePct: 1, amplitudePct: 2 }],
    ['根数为 0', { from: '2026-06-01', to: '2026-10-29', bars: 0, changePct: 1, amplitudePct: 2 }],
    ['根数是小数', { from: '2026-06-01', to: '2026-10-29', bars: 2.5, changePct: 1, amplitudePct: 2 }],
    ['涨跌是 NaN', { from: '2026-06-01', to: '2026-10-29', bars: 3, changePct: NaN, amplitudePct: 2 }],
    ['振幅是 Infinity', { from: '2026-06-01', to: '2026-10-29', bars: 3, changePct: 1, amplitudePct: Infinity }],
    ['振幅是字符串', { from: '2026-06-01', to: '2026-10-29', bars: 3, changePct: 1, amplitudePct: '2' }],
  ])('%s → 整段不写（不补 0、不截取部分字段）', (_name, range) => {
    const ctx = buildChartContext(input({ range }))
    expect(ctx).not.toBeNull()
    expect(ctx?.range).toBeUndefined()
  })
})

describe('buildChartContext · 价位只留可证的行', () => {
  it('价位为 null/空数组 → 不写 levels', () => {
    expect(buildChartContext(input({ levels: null }))?.levels).toBeUndefined()
    expect(buildChartContext(input({ levels: [] }))?.levels).toBeUndefined()
  })

  it('非有限/非正数/重复价被丢弃；同价保留第一条', () => {
    const ctx = buildChartContext(
      input({
        levels: [
          { price: 12.34, kind: 'support', label: 'a' },
          { price: 12.34, kind: 'pressure', label: 'b' },
          { price: 0, kind: 'support', label: 'zero' },
          { price: -3, kind: 'support', label: 'neg' },
          { price: NaN, kind: 'support', label: 'nan' },
          { price: '15.2', kind: 'pressure', label: 'str' },
          { price: 18, kind: 'unknown', label: 'bad-kind' },
        ],
      }),
    )
    expect(ctx?.levels).toEqual([{ price: 12.34, kind: 'support', label: 'a' }])
  })

  it(`条数上限 ${CHART_CONTEXT_MAX_LEVELS}（不把整块标注都灌进 prompt）`, () => {
    const many = Array.from({ length: 12 }, (_, i) => ({ price: 10 + i, kind: 'support', label: `L${i}` }))
    expect(buildChartContext(input({ levels: many }))?.levels).toHaveLength(CHART_CONTEXT_MAX_LEVELS)
  })

  it('label 为空 → 不写 label（不留空字符串）', () => {
    const ctx = buildChartContext(input({ levels: [{ price: 12.34, kind: 'support', label: '  ' }] }))
    expect(ctx?.levels).toEqual([{ price: 12.34, kind: 'support' }])
  })
})

describe('chartContextParts · 用户可见文案只陈述有据的部分', () => {
  it('齐全时给出类型/市场/周期/区间/价位', () => {
    const parts = chartContextParts(buildChartContext(input()) as ChartContext)
    expect(parts.join(' | ')).toContain('个股')
    expect(parts.join(' | ')).toContain('CN')
    expect(parts.join(' | ')).toContain('日线')
    expect(parts.join(' | ')).toContain('2026-06-01→2026-10-29')
    expect(parts.join(' | ')).toContain('58 根')
    expect(parts.join(' | ')).toContain('+12.34%')
    expect(parts.join(' | ')).toContain('12.34')
    expect(parts.join(' | ')).toContain('15.20')
  })

  it('没有区间/周期/价位 → 那几段不出现（不显示占位假值）', () => {
    const ctx = buildChartContext(input({ period: null, range: null, levels: null })) as ChartContext
    const text = chartContextParts(ctx).join(' | ')
    expect(text).toContain('个股')
    expect(text).not.toContain('区间')
    expect(text).not.toContain('价位')
    expect(text).not.toContain('日线')
    expect(text).not.toContain('--')
  })
})

describe('chartContextPrompt · 注入会话的文本', () => {
  it('写明标的/周期/区间/价位，并带上"用户主动提问才分析 / 不自动下单画线"的纪律', () => {
    const text = chartContextPrompt(buildChartContext(input()) as ChartContext)
    expect(text).toContain('图表上下文')
    expect(text).toContain('600111')
    expect(text).toContain('日线')
    expect(text).toContain('2026-06-01')
    expect(text).toContain('2026-10-29')
    expect(text).toContain('58 根')
    expect(text).toContain('12.34')
    expect(text).toContain('15.20')
    expect(text).toContain('进入时快照')
    expect(text).toContain('不要主动分析')
    expect(text).toContain('自动下单')
    expect(text).toContain('自动画线')
  })

  it('缺区间/价位时不写对应行（不让模型以为"用户看到了"）', () => {
    const text = chartContextPrompt(buildChartContext(input({ range: null, levels: null })) as ChartContext)
    expect(text).not.toContain('可见区间')
    expect(text).not.toContain('图上价位')
    expect(text).toContain('600111')
  })
})

describe('工作台上下文仓库 · 用户/标的/类型隔离', () => {
  beforeEach(() => clearChartContextStore())

  it('按 账号+类型+市场+代码 精确读取', () => {
    const ctx = buildChartContext(input()) as ChartContext
    const key = publishChartContext(ctx)
    expect(key).toBe(chartContextKey(ctx))
    expect(readChartContext('600111', 'CN', OWNER)?.symbol).toBe('600111')
    expect(chartContextStoreSize()).toBe(1)
  })

  it('换账号读不到（别的用户的视图不会串进来）', () => {
    publishChartContext(buildChartContext(input()) as ChartContext)
    expect(readChartContext('600111', 'CN', 'other-user')).toBeNull()
  })

  it('市场不符读不到（同号不同市场不互相证明）', () => {
    publishChartContext(buildChartContext(input()) as ChartContext)
    expect(readChartContext('600111', 'HK', OWNER)).toBeNull()
  })

  it('同号不同类型并存 → 读不到（000001 歧义不猜类型）', () => {
    publishChartContext(buildChartContext(input({ instrument: 'stock' })) as ChartContext)
    publishChartContext(buildChartContext(input({ instrument: 'index' })) as ChartContext)
    expect(chartContextStoreSize()).toBe(2)
    expect(readChartContext('600111', 'CN', OWNER)).toBeNull()
  })

  it('取消发布后读不到（换标的/离开页面即失效）', () => {
    const key = publishChartContext(buildChartContext(input()) as ChartContext)
    unpublishChartContext(key)
    expect(readChartContext('600111', 'CN', OWNER)).toBeNull()
    expect(chartContextStoreSize()).toBe(0)
  })

  it('同一 key 覆盖写（周期切换不堆积）', () => {
    publishChartContext(buildChartContext(input({ period: 'd1' })) as ChartContext)
    const ctx = buildChartContext(input({ period: 'w1' })) as ChartContext
    publishChartContext(ctx)
    expect(chartContextStoreSize()).toBe(1)
    expect(readChartContext('600111', 'CN', OWNER)?.period).toBe('w1')
  })
})
