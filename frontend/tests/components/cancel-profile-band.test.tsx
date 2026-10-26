// @vitest-environment jsdom
/**
 * I-3 委托行为画像时序带(2026-10-18)
 *
 * 守四件事:
 * ① **口径由后端给**: 一句话结论(含 PXX / 常态 X%±Ypp / z)原样展示, 前端不重算、不改写;
 * ② **今日柱可辨认**: 序列最后一根带 `cancel-profile-bar-today`, 且与 `as_of` 同日;
 * ③ **常态不报警**: |z|<1 时结论行不带告警色; z>2 的异常才用琥珀色;
 * ④ **缺数据不冒充**: `available=false` 只显示后端 reason; 单日撤单率缺值画成灰条并写明,
 *    绝不按 0 画成"这天没撤单"(0 是有效信息, 缺失不是)。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import CancelProfilePanel, { type CancelProfile } from '../../src/components/CancelProfilePanel'

function days(n: number, rate: number | null = 0.4) {
  return Array.from({ length: n }, (_, i) => ({
    date: `2026-08-${String((i % 28) + 1).padStart(2, '0')}`,
    b_rate: rate,
    s_rate: 0.35,
  }))
}

function profile(over: Partial<CancelProfile> = {}): CancelProfile {
  const series = days(60, 0.4)
  series.push({ date: '2026-10-17', b_rate: 0.62, s_rate: 0.3 })
  return {
    available: true,
    series,
    today: series[series.length - 1],
    buy: { rate: 0.62, median: 0.4, std: 0.09, p90: 0.55, min: 0.2, max: 0.62, n: 60, z: 2.44, percentile: 100 },
    sell: { rate: 0.3, median: 0.35, std: 0.05, n: 60, z: -1.0, percentile: 8 },
    verdict: { code: 'fake_bidding', label: '⚠️ 相对异常诱多', level: 'warning', abnormal: true, desc: '相对异常' },
    conclusion:
      '今日买撤 62.0%，为近 60 日 P100（常态 40.0%±9.0pp，z=+2.44, 涨幅 +4.20%） —— 撤单率显著高于该股自身常态且股价强势',
    as_of: '2026-10-17',
    window: 60,
    baseline_days: 60,
    min_baseline_days: 20,
    caliber: 'TQ get_exday_data 日线统计',
    ...over,
  }
}

afterEach(cleanup)

describe('CancelProfilePanel', () => {
  it('renders the backend conclusion verbatim and marks the latest bar as today', () => {
    render(<CancelProfilePanel profile={profile()} loading={false} failed={false} lastOkAt="" />)

    expect(screen.getByTestId('cancel-profile-conclusion').textContent).toContain('今日买撤 62.0%')
    expect(screen.getByTestId('cancel-profile-conclusion').textContent).toContain('z=+2.44')

    const bars = screen.getAllByTestId('cancel-profile-bar')
    const today = screen.getByTestId('cancel-profile-bar-today')
    expect(bars.length).toBe(60) // 61 天序列, 最新一根单独标出
    expect(today.getAttribute('data-date')).toBe('2026-10-17')

    expect(screen.getByTestId('cancel-profile-median-line')).toBeTruthy()
  })

  it('shows warning color only for a real anomaly', () => {
    const { unmount } = render(<CancelProfilePanel profile={profile()} loading={false} failed={false} lastOkAt="" />)
    expect(screen.getByTestId('cancel-profile-conclusion').className).toContain('text-amber-500')
    unmount()

    render(
      <CancelProfilePanel
        profile={profile({
          verdict: { code: 'normal', label: '常态', level: 'safe', abnormal: false, desc: '常态' },
          buy: { rate: 0.42, median: 0.4, std: 0.09, n: 60, z: 0.22, percentile: 59 },
          conclusion: '今日买撤 42.0%，为近 60 日 P59（常态 40.0%±9.0pp，z=+0.22） —— 相对该股自身常态',
        })}
        loading={false}
        failed={false}
        lastOkAt=""
      />,
    )
    expect(screen.getByTestId('cancel-profile-conclusion').className).not.toContain('text-amber-500')
    // 读数走 safeFixed: z 保留两位并带符号
    expect(screen.getByText('+0.22')).toBeTruthy()
  })

  it('renders unavailable reason instead of fabricated numbers', () => {
    render(
      <CancelProfilePanel
        profile={{ available: false, reason: 'TQ 日线统计不可用: 客户端未启动', series: [] }}
        loading={false}
        failed={false}
        lastOkAt=""
      />,
    )
    expect(screen.getByTestId('cancel-profile-unavailable').textContent).toContain('客户端未启动')
    expect(screen.queryByTestId('cancel-profile-conclusion')).toBeNull()
    expect(screen.queryByText('今日买撤')).toBeNull()
  })

  it('marks a missing daily rate as missing, never as zero', () => {
    const p = profile()
    p.series = [...days(59, 0.4), { date: '2026-10-16', b_rate: null }, { date: '2026-10-17', b_rate: 0.62 }]
    render(<CancelProfilePanel profile={p} loading={false} failed={false} lastOkAt="" />)

    const missing = screen
      .getAllByTestId('cancel-profile-bar')
      .find((el) => el.getAttribute('data-date') === '2026-10-16')
    expect(missing?.getAttribute('title')).toContain('缺值')
  })

  it('keeps the last good snapshot visible while flagging a failed refresh', () => {
    render(<CancelProfilePanel profile={profile()} loading={false} failed lastOkAt="10:31:00" />)
    expect(screen.getByTestId('cancel-profile-band')).toBeTruthy()
    expect(screen.getByText(/本次刷新失败/)).toBeTruthy()
  })

  it('shows loading and empty-failure states distinctly', () => {
    const { unmount } = render(<CancelProfilePanel profile={null} loading failed={false} lastOkAt="" />)
    expect(screen.getByTestId('cancel-profile-empty').textContent).toContain('加载中')
    unmount()

    render(<CancelProfilePanel profile={null} loading={false} failed lastOkAt="" />)
    expect(screen.getByTestId('cancel-profile-empty').textContent).toContain('取数失败')
  })
})
