// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import LadderBoard from '@panwatch/biz-ui/components/thememood/LadderBoard'

afterEach(cleanup)

const day = {
  date: '20260911',
  rows: [
    {
      boards: 2, codes: ['C'], names: ['CC'], tag: null,
      stocks: [{ symbol: 'C', name: 'CC', candle: { o: 1, h: 2, l: 1, c: 2 }, pct: 10.01, first_time: '09:32' }],
    },
    {
      boards: 1, codes: ['D'], names: ['DD'], tag: '首板',
      stocks: [{ symbol: 'D', name: 'DD', candle: null, pct: 10.0, first_time: '13:02' }],
    },
  ],
  // LadderMark extends LadderStock ⇒ `candle` 必填(此前夹具漏了, 是不合契约的假夹具)。
  blown: [{ symbol: 'Z', name: 'ZZ', candle: { o: 5, h: 6, l: 4.5, c: 5.5 }, prev_boards: 3 }],
  broken: [{ symbol: 'B', name: 'BB', candle: null, prev_boards: null }],
}

describe('LadderBoard', () => {
  it('列头全格式日期', () => {
    render(<LadderBoard ladder={[day]} liveDay={null} mode="finalized" stale={false} lastOk={null} />)
    expect(screen.getByText('2026-09-11')).toBeTruthy()
    expect(screen.queryByText('911')).toBeNull()
  })
  it('首板字样/炸板组/断板组/昨板数 tooltip', () => {
    render(<LadderBoard ladder={[day]} liveDay={null} mode="finalized" stale={false} lastOk={null} />)
    expect(screen.getByText('首板')).toBeTruthy()
    expect(screen.getByText(/炸板 1/)).toBeTruthy()
    expect(screen.getByText(/断板 1/)).toBeTruthy()
    expect(screen.getByTitle(/昨3板/)).toBeTruthy()
    expect(screen.getByTitle(/今日触板/)).toBeTruthy()
  })
  it('stale 横幅', () => {
    render(<LadderBoard ladder={[day]} liveDay={null} mode="live" stale lastOk="14:31" />)
    expect(screen.getByText(/实时源中断 14:31/)).toBeTruthy()
  })
  it('矩阵视图: 板数列 sticky left(横滑后仍能看到几板)', () => {
    render(<LadderBoard ladder={[day]} liveDay={null} mode="finalized" stale={false} lastOk={null} />)
    fireEvent.click(screen.getByRole('button', { name: '矩阵视图' }))
    const label = screen.getByText('层级')
    const col = label.parentElement as HTMLElement
    expect(col.className).toContain('sticky')
    expect(col.className).toContain('left-0')
    // 板数按钮仍在
    expect(screen.getByRole('button', { name: '2板' })).toBeTruthy()
  })
})

  // 2026-09-26 用户口径: 连板梯队默认「按日列视图」, 且最新在左。
  it('默认按日列视图, 且最新在左', () => {
    const older = { ...day, date: '20260910' }
    const newer = { ...day, date: '20260911' }
    // 故意按"旧->新"传入, 断言组件自己按日期降序排(不依赖接口顺序)
    const { container } = render(<LadderBoard ladder={[older, newer]} liveDay={null} mode="finalized" stale={false} lastOk={null} />)
    // 默认视图 = 按日列: 切换按钮此时显示的是"矩阵视图"(点它才切过去)
    expect(screen.getByRole('button', { name: '矩阵视图' })).toBeTruthy()
    const txt = container.textContent || ''
    const iNew = txt.indexOf('2026-09-11')
    const iOld = txt.indexOf('2026-09-10')
    expect(iNew).toBeGreaterThanOrEqual(0)
    expect(iNew).toBeLessThan(iOld)   // 最新在左
  })

  it('盘中 live 那天排在最左', () => {
    const live = { ...day, date: '20260912', provisional: true }
    const { container } = render(<LadderBoard ladder={[day]} liveDay={live} mode="live" stale={false} lastOk={null} />)
    const txt = container.textContent || ''
    expect(txt.indexOf('2026-09-12')).toBeLessThan(txt.indexOf('2026-09-11'))
  })
