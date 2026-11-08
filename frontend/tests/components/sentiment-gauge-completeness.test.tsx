// @vitest-environment jsdom
//
// 市场温度仪表盘(SentimentGauge)的「缺字段不得冒充完整 100 分」契约。
//
// 背景: 原实现 `metrics?.max_height ?? 0` 把 null 当 0 代入
// `高度×15 + 晋级率×40 + 封板率×45`, 缺两项时仍给出一个 0-100 的分数并由指针指向它,
// 读起来就是「完整口径算出来的市场温度」。缺字段与真 0 必须分开:
//   - 缺任一关键量(null/undefined/NaN) ⇒ 不显示分数, 显式「不完整」+ 缺哪几项;
//   - 0 是真值(高度 0 板 / 晋级率 0% 都合法) ⇒ 照常算分、照常显示;
//   - 三项齐全时算法与历史完全一致(高度×15 + 晋级率×40 + 封板率×45, 上限 100)。
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ setOption: vi.fn() }))

vi.mock('@sida/biz-ui/hooks/useECharts', () => ({
  useECharts: () => ({
    ref: () => {},
    chartRef: { current: { setOption: mocks.setOption, resize: vi.fn() } },
  }),
}))
vi.mock('@sida/biz-ui/lib/echarts-core', () => ({
  default: { graphic: { LinearGradient: class {} } },
}))

import SentimentGauge, { computeGaugeScore } from '@sida/biz-ui/components/dashboard/SentimentGauge'

beforeEach(() => mocks.setOption.mockReset())
afterEach(() => cleanup())

const series = () => {
  const opt = mocks.setOption.mock.calls.at(-1)?.[0] as { series?: Array<Record<string, unknown>> } | undefined
  return opt?.series?.[0] as Record<string, any> | undefined
}

describe('SentimentGauge 完整性', () => {
  it('三项齐全 → 沿用原算法(高度×15 + 晋级率×40 + 封板率×45)', () => {
    // 5*15 + 0.3*40 + 0.6*45 = 75 + 12 + 27 = 114 → 上限 100
    expect(computeGaugeScore({ max_height: 5, promo_rate: 0.3, seal_rate: 0.6 })).toEqual({
      score: 100,
      missing: [],
      complete: true,
    })
    // 未触顶: 3*15 + 0.5*40 + 0.2*45 = 45 + 20 + 9 = 74
    expect(computeGaugeScore({ max_height: 3, promo_rate: 0.5, seal_rate: 0.2 }).score).toBe(74)
  })

  it('0 是真值: 三项全 0 → 完整、分数 0, 不标不完整', () => {
    const r = computeGaugeScore({ max_height: 0, promo_rate: 0, seal_rate: 0 })
    expect(r).toEqual({ score: 0, missing: [], complete: true })

    render(<SentimentGauge phase="ice" metrics={{ max_height: 0, promo_rate: 0, seal_rate: 0 }} />)
    const box = screen.getByTestId('sentiment-gauge')
    expect(box.getAttribute('data-gauge-complete')).toBe('true')
    expect(screen.getByText('0')).toBeTruthy()
    expect(screen.queryByText(/不完整/)).toBeNull()
  })

  it('缺一项 → 不显示分数, 显式不完整并点名缺失项', () => {
    render(<SentimentGauge phase="rally" metrics={{ max_height: 4, promo_rate: null, seal_rate: 0.6 }} />)
    const box = screen.getByTestId('sentiment-gauge')
    expect(box.getAttribute('data-gauge-complete')).toBe('false')
    expect(box.getAttribute('data-gauge-missing')).toBe('promo_rate')
    // 不冒充: 旧算法会给出 round(60 + 0 + 27) = 87
    expect(screen.queryByText('87')).toBeNull()
    expect(screen.getByText('--')).toBeTruthy()
    expect(screen.getByTestId('gauge-status').textContent).toBe('不完整 · 缺晋级率')
  })

  it('三项全缺(metrics=null) → 全部点名, 不显示 0 分', () => {
    render(<SentimentGauge phase={null} metrics={null} />)
    const box = screen.getByTestId('sentiment-gauge')
    expect(box.getAttribute('data-gauge-missing')).toBe('max_height promo_rate seal_rate')
    expect(screen.queryByText('0')).toBeNull()
    expect(screen.getByText('--')).toBeTruthy()
    expect(screen.getByTestId('gauge-status').textContent).toBe('不完整 · 缺高度/晋级率/封板率')
  })

  it('不完整时表盘不指指针(不给缺项数据一个伪造落点)', () => {
    render(<SentimentGauge phase="rally" metrics={{ max_height: 4, promo_rate: null, seal_rate: 0.6 }} />)
    expect(series()?.pointer?.show).toBe(false)
    expect(series()?.progress?.show).toBe(false)
  })

  it('完整时表盘照旧: 指针可见、值为算出的分数', () => {
    render(<SentimentGauge phase="rally" metrics={{ max_height: 3, promo_rate: 0.5, seal_rate: 0.2 }} />)
    expect(series()?.pointer?.show).toBe(true)
    expect(series()?.data?.[0]?.value).toBe(74)
  })
})
