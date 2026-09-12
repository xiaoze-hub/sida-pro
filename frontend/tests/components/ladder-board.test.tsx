// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
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
  blown: [{ symbol: 'Z', name: 'ZZ', prev_boards: 3 }],
  broken: [{ symbol: 'B', name: 'BB', prev_boards: null }],
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
})
