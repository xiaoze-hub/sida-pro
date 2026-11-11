// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 首页「市场主线」并列视图布局 (2026-11-11)。
 *
 * 背景: 卡片真实容器只有 ~400px(Dashboard 三栏 `lg:grid-cols-3` 中的一栏), 但并列视图
 * 内部又用**视口断点** `md:grid-cols-2` 拆两列 ⇒ 每列 ~190px。此时 名称 / 得分 / 家数 /
 * 最高板 / 龙头 全是 `shrink-0` 固定块, 名称被挤到 0px、指标互相叠字、行横向溢出被
 * `overflow-hidden` 硬切(真机 Chromium 量到: 名称可见宽 0px, 「82.4」与「13家」相交 130px²)。
 *
 * 这里的断言钉住**布局契约**(jsdom 无排版引擎, 几何证据由 Chromium 探针给):
 *   1) 并列列数必须跟随**容器宽度**(container query), 不许跟视口断点;
 *   2) 每行五个字段各有独立槽位, 名称槽位可收缩+省略, 不许被硬切;
 *   3) 指标 / 龙头不许按视口 `hidden` 隐藏(窄容器下也必须读得到);
 *   4) 样式契约文件里有对应的 container query 与 min-width:0/ellipsis 规则。
 */

const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@sida/api', () => ({
  fetchAPI: (...args: unknown[]) => mocks.fetchAPI(...args),
}))

import MarketMainlineCard from '@sida/biz-ui/components/MarketMainlineCard'

const LONG_ZH = '半导体设备与材料国产替代'
const LONG_EN = 'Semiconductor Equipment & Materials Supply Chain'

function group(name: string, dimension: 'industry' | 'concept') {
  return {
    name,
    dimension,
    limit_up_count: 12,
    ge2_count: 4,
    max_boards: 6,
    boards_sum: 21,
    rungs: 4,
    score: 82.4,
    leader: { code: '600001', name: `${name}龙头`, days: 5, amount: 8.8e8 },
    constituents: [{ code: '600001', name: `${name}龙头`, days: 5, amount: 8.8e8, concept_count: 2 }],
    leader_shared_concepts: ['卫星互联网'],
    leader_basis: '本概念内排序第一，非该股专属驱动',
    exclusive_ratio: 0.5,
  }
}

const payload = {
  total_groups: 4,
  total_limit_ups: 33,
  min_limitup_for_rank: 3,
  ranked_groups: [group(LONG_ZH, 'industry'), group(LONG_EN, 'industry'), group('光伏储能', 'industry')],
  unranked: [],
  filter_stats: { broad_filtered: 1, below_min: 1, ranked: 3 },
  concept_groups: [group(`${LONG_ZH}概念`, 'concept'), group('商业航天', 'concept')],
  concept_unranked: [],
  concept_stats: {
    distinct_stocks: 12,
    member_count_sum: 17,
    overlap_excess: 5,
    min_limitup_for_rank: 3,
    summable: false,
  },
  concept_status: 'available',
  data_meta: { as_of: '2026-11-11', computed_at: '2026-11-11T14:32:10+08:00', pool_source: 'tq', concept_source: 'tq:get_relation' },
  note: '',
}

const CSS_PATH = resolve(__dirname, '../../packages/biz-ui/src/components/market-mainline.css')

beforeEach(() => {
  mocks.fetchAPI.mockReset()
  mocks.fetchAPI.mockResolvedValue(payload)
})

afterEach(() => cleanup())

async function renderBoth() {
  render(<MarketMainlineCard />)
  await screen.findByText(LONG_ZH)
  return screen.getByTestId('mainline-both-grid')
}

describe('市场主线 · 并列视图布局契约', () => {
  it('并列列数跟随容器宽度(container query), 不用视口断点 md:grid-cols-2', async () => {
    const grid = await renderBoth()
    expect(grid.className).toContain('mainline-both-grid')
    expect(grid.className).not.toMatch(/(^|\s)md:grid-cols-2(\s|$)/)
    expect(grid.className).not.toMatch(/(^|\s)grid-cols-2(\s|$)/) // 无条件两列同样会让 190px 列出现
  })

  it('两列各自是 container-type 容器, 行按列宽自适应', async () => {
    await renderBoth()
    const cols = screen.getAllByTestId('mainline-col')
    expect(cols).toHaveLength(2)
    cols.forEach((c) => expect(c.className).toContain('mainline-col'))
  })

  it('每行: 名称/得分/家数/板/档/龙头 各有独立槽位, 名称可收缩+省略', async () => {
    await renderBoth()
    const rows = screen.getAllByTestId('mainline-row')
    expect(rows.length).toBeGreaterThan(0)
    const row = rows[0]
    const name = row.querySelector('[data-mainline-name]') as HTMLElement
    const score = row.querySelector('[data-mainline-score]') as HTMLElement
    const metrics = row.querySelector('[data-mainline-metrics]') as HTMLElement
    const leader = row.querySelector('[data-mainline-leader]') as HTMLElement
    expect(name).toBeTruthy()
    expect(score).toBeTruthy()
    expect(metrics).toBeTruthy()
    expect(leader).toBeTruthy()
    // 名称必须能收缩 + 省略, 否则 flex 会把它压成 0px 再把邻居挤出去
    expect(name.className).toContain('min-w-0')
    expect(name.getAttribute('title')).toBe(LONG_ZH)
    expect(within(metrics).getByText(/家/)).toBeTruthy()
    expect(within(metrics).getByText(/板/)).toBeTruthy()
    expect(within(metrics).getByText(/档/)).toBeTruthy()
  })

  it('指标与龙头不按视口隐藏: 窄容器也要能读到龙头', async () => {
    await renderBoth()
    const rows = screen.getAllByTestId('mainline-row')
    rows.forEach((row) => {
      for (const sel of ['[data-mainline-metrics]', '[data-mainline-leader]']) {
        const el = row.querySelector(sel) as HTMLElement
        expect(el).toBeTruthy()
        expect(el.className).not.toMatch(/(^|\s)hidden(\s|$)/)
      }
    })
    expect(within(rows[0]).getAllByText(/60|龙头|航天|半导体/).length).toBeGreaterThan(0)
  })

  it('行不再用 overflow-hidden 硬切文字(文字要换行/省略, 不能被切掉)', async () => {
    await renderBoth()
    const rows = screen.getAllByTestId('mainline-row')
    rows.forEach((row) => {
      const wrap = row.closest('[data-mainline-row-wrap]') as HTMLElement
      expect(wrap).toBeTruthy()
      expect(wrap.className).not.toContain('overflow-hidden')
    })
  })

  it('单列模式(行业页签)仍是一列, 不分栏', async () => {
    render(<MarketMainlineCard />)
    await screen.findByText(LONG_ZH)
    fireEvent.click(screen.getByRole('tab', { name: /行业/ }))
    expect(screen.getAllByTestId('mainline-col')).toHaveLength(1)
  })

  it('样式契约: 容器查询 + 名称可收缩省略 + 行不裁剪', () => {
    const css = readFileSync(CSS_PATH, 'utf8')
    expect(css).toMatch(/container-type:\s*inline-size/)
    // 并列两列的阈值按**卡片**宽度, 行内排版阈值按**列**宽度
    expect(css).toMatch(/@container\s+[\w-]+\s*\(min-width:\s*\d+px\)/)
    expect(css).toContain('.mainline-both-grid')
    expect(css).toMatch(/\.mainline-row__name[^{]*\{[^}]*min-width:\s*0/s)
    expect(css).toMatch(/\.mainline-row__name[^{]*\{[^}]*text-overflow:\s*ellipsis/s)
    // 行/行主体/指标行本身不许裁剪内容(名称槽位自己 overflow:hidden 做省略是另一回事)
    expect(css).not.toMatch(/\.mainline-row\s*\{[^}]*overflow:\s*hidden/s)
    expect(css).not.toMatch(/\.mainline-row__(body|metrics)\s*\{[^}]*overflow:\s*hidden/s)
  })

  it('展开态: 成分股摘要行必须可换行(窄列里不许被裁/溢出)', async () => {
    await renderBoth()
    const rows = screen.getAllByTestId('mainline-row')
    fireEvent.click(rows[0])
    // 成分股摘要(成分股 N 只 · 成交额 … · 独占 …)在 200px 列里放不进一行
    const summary = screen.getByText(/成分股 \d+ 只/)
    const line = summary.parentElement as HTMLElement
    expect(line.className).toContain('flex-wrap')
  })

  it('加载态(无 action)标题仍是唯一头节点: 不能被 action 样式右对齐/压宽', async () => {
    mocks.fetchAPI.mockReturnValue(new Promise(() => {})) // 永不 resolve → 停在加载态
    render(<MarketMainlineCard />)
    const card = screen.getByRole('status')
    const header = card.querySelector('.mainline-header') as HTMLElement
    expect(header).toBeTruthy()
    expect(header.children).toHaveLength(1) // 只有标题块, 没有 action
    const css = readFileSync(CSS_PATH, 'utf8')
    // 命中 action 只能用「第二子节点」, 不许用 :last-child(无 action 时会打到标题)
    expect(css).toMatch(/\.mainline-header\s*>\s*div\s*\+\s*div/)
    expect(css).not.toMatch(/\.mainline-header\s*>\s*div:last-child/)
  })

  it('失败空态(无 updatedAt → 无 action)标题同样不被 action 样式命中', async () => {
    mocks.fetchAPI.mockRejectedValue(new Error('network down'))
    render(<MarketMainlineCard />)
    const empty = await screen.findByTestId('mainline-empty')
    const header = empty.querySelector('.mainline-header') as HTMLElement
    expect(header).toBeTruthy()
    expect(header.children).toHaveLength(1)
    expect(empty.textContent).toContain('network down')
  })
})
