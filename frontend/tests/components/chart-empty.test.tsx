// @vitest-environment jsdom
//
// UI 走查 B2(2026-09-18):「无数据时图表仍占巨幅空白」的回归。
//  - ChartEmpty: 紧凑高度 + 说明 + 下一步, 且**绝不把 0 当无数据**;
//  - BoardHeatmap 空态: 必须用紧凑高度(源级钉, 因为渲染它要 mock 取数 hook);
//  - ApiKeys 空态: 走 compact。
import { readFileSync } from 'fs'
import { resolve } from 'path'

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { ChartEmpty } from '@/components/ChartEmpty'

afterEach(cleanup)

describe('ChartEmpty(B2 图表空态)', () => {
  it('渲染标题/说明/下一步, 带 data 标记', () => {
    render(
      <ChartEmpty
        title="暂无足够数据绘制曲线"
        description="完成一笔模拟交易后会出现"
        action={<button type="button">去模拟交易</button>}
      />,
    )
    const box = document.querySelector('[data-chart-empty="1"]') as HTMLElement
    expect(box).toBeTruthy()
    expect(screen.getByText('暂无足够数据绘制曲线')).toBeTruthy()
    expect(screen.getByText('完成一笔模拟交易后会出现')).toBeTruthy()
    expect(screen.getByRole('button', { name: '去模拟交易' })).toBeTruthy()
  })

  it('默认高度紧凑(132px), 不是整屏空白', () => {
    render(<ChartEmpty />)
    const box = document.querySelector('[data-chart-empty="1"]') as HTMLElement
    expect(box.style.minHeight).toBe('132px')
  })

  it('可以说"没有数据", 但绝不显示 0 当作"无数据"', () => {
    render(<ChartEmpty />)
    const box = document.querySelector('[data-chart-empty="1"]') as HTMLElement
    expect(box.textContent).toContain('暂无数据')
    expect((box.textContent || '').replace(/132|168/g, '')).not.toMatch(/\b0\b/)
  })
})

describe('空态压缩落在页面上(B2)', () => {
  it('BoardHeatmap 空态高度必须远小于有数据时的高度', () => {
    const src = readFileSync(
      resolve(__dirname, '../../packages/biz-ui/src/components/dashboard/BoardHeatmap.tsx'),
      'utf-8',
    )
    const full = Number(/const CHART_HEIGHT = (\d+)/.exec(src)?.[1] ?? '0')
    const empty = Number(/const CHART_EMPTY_HEIGHT = (\d+)/.exec(src)?.[1] ?? '0')
    expect(full).toBeGreaterThan(400)
    expect(empty).toBeGreaterThan(80)
    expect(empty).toBeLessThanOrEqual(240)
    // 空态分支必须用紧凑高度(不能再用 CHART_HEIGHT)
    const emptyBlock = src.slice(src.indexOf('data-testid="heatmap-empty"'))
    expect(emptyBlock.slice(0, 400)).toContain('CHART_EMPTY_HEIGHT')
  })

  it('PaperTrading 空曲线走 ChartEmpty(不再写死 h-48 空白)', () => {
    const src = readFileSync(resolve(__dirname, '../../src/pages/PaperTrading.tsx'), 'utf-8')
    expect(src).toContain('ChartEmpty')
    expect(src).not.toContain('h-48 flex items-center justify-center text-muted-foreground text-sm')
  })

  it('ApiKeys 空态用 compact', () => {
    const src = readFileSync(resolve(__dirname, '../../src/pages/ApiKeys.tsx'), 'utf-8')
    const block = src.slice(src.indexOf('还没有 API Key') - 200, src.indexOf('还没有 API Key') + 200)
    expect(block).toContain('compact')
  })
})
