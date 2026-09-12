// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import DayCandle from '@panwatch/biz-ui/components/thememood/DayCandle'

afterEach(cleanup)

describe('DayCandle', () => {
  it('阳线渲染 svg 且 aria-label 阳柱', () => {
    render(<DayCandle candle={{ o: 1, h: 3, l: 0.5, c: 2 }} basis="qfq" />)
    expect(screen.getByLabelText('阳柱')).toBeTruthy()
  })
  it('阴线 aria-label 阴柱', () => {
    render(<DayCandle candle={{ o: 2, h: 3, l: 0.5, c: 1 }} basis="raw" />)
    expect(screen.getByLabelText('阴柱')).toBeTruthy()
  })
  it('无K不画影线, 给占位与说明', () => {
    render(<DayCandle candle={null} basis="qfq" />)
    expect(screen.queryByLabelText('阳柱')).toBeNull()
    expect(screen.queryByLabelText('阴柱')).toBeNull()
    expect(screen.getByTitle('无K数据')).toBeTruthy()
  })
})
