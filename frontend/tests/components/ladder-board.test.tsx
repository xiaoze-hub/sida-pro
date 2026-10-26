// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import LadderBoard from '@panwatch/biz-ui/components/thememood/LadderBoard'

afterEach(() => {
  cleanup()
  localStorage.clear()
})

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
    fireEvent.click(screen.getByRole('button', { name: '按日列视图' }))
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
    const label = screen.getByText('层级')
    const col = label.parentElement as HTMLElement
    expect(col.className).toContain('sticky')
    expect(col.className).toContain('left-0')
    // 板数按钮仍在
    expect(screen.getByRole('button', { name: '2板' })).toBeTruthy()
  })
})

  // 2026-09-26 用户口径: 连板梯队默认「矩阵视图」天梯, 且最新在左。
  it('默认矩阵视图, 且最新在左', () => {
    const older = { ...day, date: '20260910' }
    const newer = { ...day, date: '20260911' }
    // 故意按"旧->新"传入, 断言组件自己按日期降序排(不依赖接口顺序)
    const { container } = render(<LadderBoard ladder={[older, newer]} liveDay={null} mode="finalized" stale={false} lastOk={null} />)
    // 默认视图 = 矩阵天梯: 切换按钮此时显示的是"按日列视图"(点它才切到日列)
    expect(screen.getByRole('button', { name: '按日列视图' })).toBeTruthy()
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

  // 2026-10-11 首封×封成比 强度徽章(影子运行): 有标签出徽章, 无标签不出(降级不编造)。
  it('强度徽章: 有标签出、无标签不出', () => {
    const d = {
      ...day,
      date: '20260920',
      rows: [
        {
          boards: 1, codes: ['E'], names: ['EE'], tag: '首板',
          stocks: [{ symbol: 'E', name: 'EE', candle: null, pct: 10.0, strength_label: 'S' }],
        },
        {
          boards: 1, codes: ['F'], names: ['FF'], tag: '首板',
          stocks: [{ symbol: 'F', name: 'FF', candle: null, pct: 10.0 }],
        },
      ],
    }
    render(<LadderBoard ladder={[d]} liveDay={null} mode="finalized" stale={false} lastOk={null} />)
    const badge = screen.getByTestId('ladder-strength-E')
    expect(badge.textContent).toBe('S')
    expect(badge.getAttribute('title')).toContain('影子运行')
    expect(screen.queryByTestId('ladder-strength-F')).toBeNull()
  })

  // 2026-10-13 盘中徽章: live 列带「盘中 · 截至 xx:xx」小字(不卡片化); 定型后徽章消失。
  it('盘中 live 列带「盘中 · 截至 xx:xx」徽章, 定型后无', () => {
    const live = { ...day, date: '20260912', provisional: true }
    const { rerender } = render(
      <LadderBoard ladder={[day]} liveDay={live} mode="live" stale={false} lastOk="14:31" liveAsOf="14:31" />,
    )
    expect(screen.getByText('盘中 · 截至 14:31')).toBeTruthy()
    // 定型后不再有 live 列 ⇒ 徽章消失
    rerender(
      <LadderBoard ladder={[day]} liveDay={null} mode="finalized" stale={false} lastOk={null} liveAsOf={null} />,
    )
    expect(screen.queryAllByText(/盘中/)).toHaveLength(0)
  })

  // P0(2026-10-18 审计 F06): 缺 pct 不得渲染 `--`(单页 820 处), 但格子高度要保持。
  it('pct 为空时不渲染 -- 占位, 有值时正常显示', () => {
    const d = {
      ...day,
      date: '20260921',
      rows: [
        {
          boards: 1, codes: ['G', 'H'], names: ['GG', 'HH'], tag: '首板',
          stocks: [
            { symbol: 'G', name: 'GG', candle: null, pct: null },
            { symbol: 'H', name: 'HH', candle: null, pct: 10.02 },
          ],
        },
      ],
    }
    const { container } = render(<LadderBoard ladder={[d]} liveDay={null} mode="finalized" stale={false} lastOk={null} />)
    expect(container.textContent).not.toContain('--')
    expect(container.textContent).toContain('+10.02%')
    // 缺值那格仍渲染(只是没有涨跌幅), 名称在
    expect(container.querySelector('[data-ladder-stock="G"]')).toBeTruthy()
  })
