// @vitest-environment jsdom
/**
 * 走查 P3-2(2026-10-01 UI 全站走查): 截断文本必须可悬停看全。
 *
 * 现场: 全站 192(通知列表)+14(深度分析目录)+theme-mood/历史/设置 若干处
 * `text-overflow: ellipsis` / `truncate` 元素既无 `title` 也无 `aria-label` ——
 * 用户鼠标悬停看不到全称, 只能猜。
 *
 * 修法(本次): 只给截断元素补 `title`(值 = 完整文本), 不改任何视觉/尺寸/配色。
 * 本文件钉住两件事:
 *   ① DOM 层 —— 挂载真实组件, 断言截断元素的 `title` **存在且等于完整文本**
 *      (不是被 CSS 省略号截掉的可见片段);
 *   ② 源级契约 —— 通知列表 / 深度分析目录这些"重数据页"的关键截断点带 title,
 *      防止以后改回去(repo 既有做法, 见 missing-fields.test.tsx)。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))
vi.mock('@panwatch/api', () => ({ fetchAPI: (...a: unknown[]) => mocks.fetchAPI(...a) }))

import MissingFields from '@/components/MissingFields'
import ThemeMoodPage from '@/pages/ThemeMood'
import Stat from '@panwatch/biz-ui/components/Stat'
import SectionHeader from '@panwatch/biz-ui/components/SectionHeader'
import LadderBoard from '@panwatch/biz-ui/components/thememood/LadderBoard'

afterEach(cleanup)

const src = (p: string) => readFileSync(resolve(__dirname, '../..', p), 'utf-8')

/** 容器内所有"会被省略号截断"的元素(class 含独立 token `truncate`)。 */
const truncatedEls = (c: HTMLElement) =>
  Array.from(c.querySelectorAll<HTMLElement>('[class*="truncate"]')).filter((el) =>
    /(^|\s)truncate(\s|$)/.test(el.getAttribute('class') || ''),
  )

/** 断言: 每个截断元素的 title 都等于它的完整文本(逐字相等)。 */
const expectTitlesEqualText = (c: HTMLElement, expectedCount: number) => {
  const els = truncatedEls(c)
  expect(els.length).toBe(expectedCount)
  for (const el of els) {
    const text = (el.textContent || '').trim()
    expect(text.length, '截断元素应有可见文本').toBeGreaterThan(0)
    // 关键断言: title 存在, 且等于完整文本(不是被截断的片段)
    expect(el.getAttribute('title')).toBe(text)
  }
}

// 故意用超长字符串: 证明 title 携带的是**完整**文本, 不是可见的那一截。
const LONG = '半导体设备与材料国产替代产业链(含光刻胶/EDA/大硅片全环节)'
const LONG2 = '2026-08-06 深度分析 · 神剑决策 · 置信度 7.2/10 · 成本 $0.4123'
const LONG3 = '主力净流入 +12.34 亿 · 成交额 8765.4 亿 · 情绪周期: 主升'

describe('截断文本的 title 契约(DOM)', () => {
  it('Stat: label/value/sub 三处截断都带 title 且等于完整文本', () => {
    const { container } = render(<Stat label={LONG} value={LONG2} sub={LONG3} />)
    expectTitlesEqualText(container, 3)
  })

  it('SectionHeader: 截断的标题带 title 且等于完整文本', () => {
    const { container } = render(<SectionHeader title={LONG} />)
    expectTitlesEqualText(container, 1)
  })

  it('MissingFields: 折叠行截断文本的 title = head + 原因摘要(完整文本)', () => {
    const { container } = render(
      <MissingFields
        title={LONG}
        items={[
          { label: '十档买卖额', reason: 'TQ 未连接' },
          { label: '封单成色', reason: '当日无成交' },
        ]}
      />,
    )
    expectTitlesEqualText(container, 1)
    const el = truncatedEls(container)[0]
    expect(el.getAttribute('title')).toContain('本页缺 2 项')
  })

  it('LadderBoard: 个股名截断单元格的 title = 完整个股名', () => {
    const day = {
      date: '20260911',
      rows: [
        {
          boards: 1, codes: ['D'], names: ['DD'], tag: '首板',
          stocks: [{ symbol: 'D', name: LONG, candle: { o: 1, h: 2, l: 1, c: 2 }, pct: 10, first_time: '09:32' }],
        },
      ],
      blown: [], broken: [],
    }
    render(<LadderBoard ladder={[day]} liveDay={null} mode="finalized" stale={false} lastOk={null} />)
    const cell = screen.getByText(LONG)
    expect(cell.getAttribute('title')).toBe(LONG)
    expect(cell.getAttribute('class')).toContain('truncate')
  })
})

describe('截断文本的 title 契约(真实页面 · theme-mood)', () => {
  beforeEach(() => mocks.fetchAPI.mockReset())

  it('题材列表的行名被截断时 title = 完整题材名', async () => {
    const resp = {
      trade_date: '20260911', window: 20, count: 1,
      dates: ['20260911'],
      market: [{ date: '20260911', score: 71.7 }],
      items: [
        {
          block_code: '881101.SH', block_name: LONG, block_type: 'industry', score: 78.2, delta: 4.1,
          confidence: 86, core: true, s1: 80, s2: 75, s3: 82, s4: 70, s5: 60, limit_up_cnt: 9, max_boards: 3,
          core_stocks: [], cells: [{ date: '20260911', score: 78.2, limit_up_cnt: 9 }],
        },
      ],
    }
    mocks.fetchAPI.mockResolvedValue(resp)
    render(<ThemeMoodPage />)
    await screen.findAllByText(LONG)
    const cell = screen.getAllByText(LONG).find((el) => (el.getAttribute('class') || '').includes('truncate'))
    expect(cell, '题材行名应是 truncate 元素').toBeTruthy()
    expect(cell!.getAttribute('title')).toBe(LONG)
  })
})

describe('截断文本的 title 契约(源级 · 重数据页)', () => {
  it('通知列表: 标题 / 正文 / 渠道摘要三处截断都补了 title', () => {
    const s = src('src/pages/Notifications.tsx')
    expect(s).toMatch(/<span title=\{item\.title \|\| '未命名通知'\} className=\{`truncate/)
    expect(s).toMatch(/title=\{item\.body \|\| '无正文'\}/)
    expect(s).toMatch(/title=\{channelSummary\(item, t\)\}/)
  })

  it('深度分析目录: 目录按钮 + 页标题 + 移动端吸顶条都补了 title', () => {
    const s = src('src/pages/AnalysisDetail.tsx')
    expect(s).toMatch(/title=\{t\.title\}[\s\S]{0,120}className=\{`block w-full text-left[\s\S]{0,80}truncate/)
    // perf(2026-10-02): 页标题在加载期即渲染(result 可能为 null) → result?.title;
    // 截断 + title 兜底的契约不变(仅 nullable 取值)。
    expect(s).toMatch(/title=\{result\?\.title \|\| `\$\{symbol\} 深度分析`\}/)
    expect(s).toMatch(/title=\{currentTitle \|\| '目录'\}/)
  })

  it('历史 / 报告列表: 记录标题截断补了 title', () => {
    expect(src('src/pages/History.tsx')).toMatch(/title=\{formatTitle\(selectedRecord\)\}/)
    const rep = src('src/pages/Reports.tsx')
    expect(rep).toMatch(/title=\{jobName\}/)
    expect(rep).toMatch(/title=\{it\.title_preview \|\| it\.file\}/)
  })

  it('设置页 AI 服务商: base_url / 描述截断补了 title', () => {
    const ai = src('src/pages/settings/AiSection.tsx')
    expect(ai).toMatch(/title=\{svc\.base_url\}/)
    expect(ai).toMatch(/title=\{b\.description\}/)
  })
})
