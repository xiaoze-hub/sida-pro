// @vitest-environment jsdom
/**
 * 连板梯队 StockChip 契约测试 — 2026-10-27 修「涨幅只显示一半」
 *
 * 成因（实测复现过）：格子固定 52×64px，但 chip 内容有三行基础信息(K线 26px + 名称 + 涨幅)
 * 加徽章后高达 93px，被单元格 `overflow-hidden` 从底部切掉 —— 被切掉的正好是最下面那行涨幅。
 * 另外 px-0.5 把内容区压到 46px，而 `+10.01%` 要 48px，左右各溢出 1px。
 *
 * 锁死：
 *  ① 涨幅文本必须**完整**渲染成独立节点（不是被省略号截断的字符串）；
 *  ② 矩阵视图(64px 高密集型)不渲染徽章 —— 否则撑破格子又回到被裁的老样子；
 *  ③ 按日列视图(高度自适应)保留徽章，信息不丢。
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import LadderBoard, { type LadderDay } from '@panwatch/biz-ui/components/thememood/LadderBoard'

const candle = { o: 10, h: 11, l: 9.8, c: 11 }

const stock = (symbol: string, name: string, pct: number, extra: Record<string, unknown> = {}) => ({
  symbol,
  name,
  candle,
  pct,
  ...extra,
})

const day = (date: string, stocks: ReturnType<typeof stock>[]): LadderDay => ({
  date,
  rows: [{ boards: 2, codes: stocks.map((s) => s.symbol), names: stocks.map((s) => s.name), tag: null, stocks }],
  blown: [],
  broken: [],
})

beforeEach(() => {
  window.localStorage.clear()
})

afterEach(cleanup)

describe('连板梯队涨幅不被裁', () => {
  it('涨幅以完整字符串渲染(含正负号与百分号)', () => {
    render(
      <LadderBoard
        ladder={[day('2026-10-24', [stock('600111', '北方稀土', 10.01)])]}
        liveDay={null}
        mode="finalized"
        stale={false}
        lastOk={null}
      />,
    )
    const chip = document.querySelector('[data-ladder-stock="600111"]') as HTMLElement
    expect(chip).toBeTruthy()
    const pct = Array.from(chip.querySelectorAll('span')).find((el) => el.textContent === '+10.01%')
    expect(pct).toBeTruthy()
    // 涨幅行不能再靠默认行高(18px)把整格撑出 64px 之外
    expect(pct!.className).toContain('leading-[14px]')
    expect(pct!.className).toContain('whitespace-nowrap')
  })

  it('矩阵视图不带徽章(64px 格子里塞不下, 会挤掉涨幅)', () => {
    render(
      <LadderBoard
        ladder={[day('2026-10-24', [stock('600111', '北方稀土', 10.01, { strength_label: 'S', tag: '首封' })])]}
        liveDay={null}
        mode="finalized"
        stale={false}
        lastOk={null}
      />,
    )
    expect(screen.queryByTestId('ladder-strength-600111')).toBeNull()
  })

  it('切到按日列视图后徽章回来(信息不丢, 只是不在密集矩阵里重复)', () => {
    render(
      <LadderBoard
        ladder={[day('2026-10-24', [stock('600111', '北方稀土', 10.01, { strength_label: 'S', tag: '首封' })])]}
        liveDay={null}
        mode="finalized"
        stale={false}
        lastOk={null}
      />,
    )
    fireEvent.click(screen.getByText('按日列视图'))
    expect(screen.getByTestId('ladder-strength-600111')).toBeTruthy()
    expect(screen.getByText('首封')).toBeTruthy()
  })

  it('负涨幅也完整渲染', () => {
    render(
      <LadderBoard
        ladder={[day('2026-10-24', [stock('600222', '太龙药业', -10.01)])]}
        liveDay={null}
        mode="finalized"
        stale={false}
        lastOk={null}
      />,
    )
    const chip = document.querySelector('[data-ladder-stock="600222"]') as HTMLElement
    const pct = Array.from(chip.querySelectorAll('span')).find((el) => el.textContent === '-10.01%')
    expect(pct).toBeTruthy()
  })
})
