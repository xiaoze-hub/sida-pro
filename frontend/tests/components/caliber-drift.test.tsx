// @vitest-environment jsdom
//
// 口径漂移区(B5b, 2026-09-18) —— 逐日留痕, **不合成单一权威数字**。
//
// 钉五件事:
//  ① 每源各一列, 值原样并存(100/150/200 不会变成平均值 150);
//  ② 没留痕的日期显示「该日未留痕」, **绝不显示 0**;
//  ③ 空留痕 → 说明"交易日 15:55 采集", 且明说不会用 0/推算值填补;
//  ④ 跨源差异写明**比的是哪两个字段** + "口径差异不是误差" + 两端都有几天(样本量);
//  ⑤ 样本不足(0 天)时不给差异统计, 而不是给 0。
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import CaliberComparePage from '@/pages/CaliberCompare'

const { compareMock, driftMock } = vi.hoisted(() => ({
  compareMock: vi.fn(),
  driftMock: vi.fn(),
}))

vi.mock('@panwatch/api', () => ({
  caliberCompareApi: { get: compareMock },
  caliberDriftApi: { get: driftMock },
}))

const COMPARE = {
  symbol: '002361',
  market: 'CN',
  as_of: '2026-09-18T16:30:00+08:00',
  available_count: 1,
  sources: [
    {
      key: 'thsdk_l2',
      name: '明盘 L2 主力净流入（TQ / 同花顺口径）',
      caliber: '按单笔成交金额分档汇总。',
      unit: '元',
      available: true,
      fields: [{ label: '主力净流入', value: 1e8 }],
      note: '',
    },
  ],
  differences: [],
}

const FIELDS = {
  thsdk_l2: '主力净流入',
  tencent_dark: '主力净额（≥20万）',
  eastmoney_flow: '主力净流入',
}

const DRIFT_WITH_DATA = {
  symbol: '002361',
  days: 30,
  field_by_source: FIELDS,
  series: [
    {
      trade_date: '2026-09-17',
      sources: {
        thsdk_l2: { value: 1e8, available: true, reason: '', quality: '', unit: '元' },
        tencent_dark: { value: 1.5e8, available: true, reason: '', quality: 'suspect', unit: '元' },
        eastmoney_flow: { value: 2e8, available: true, reason: '', quality: '', unit: '元' },
      },
    },
    {
      trade_date: '2026-09-16',
      sources: {
        thsdk_l2: { value: 1e8, available: true, reason: '', quality: '', unit: '元' },
        tencent_dark: { value: null, available: false, reason: '该日未留痕', quality: '', unit: '' },
        eastmoney_flow: { value: -5e7, available: true, reason: '', quality: '', unit: '元' },
      },
    },
  ],
  comparisons: [
    {
      left: { source: 'thsdk_l2', field: '主力净流入' },
      right: { source: 'eastmoney_flow', field: '主力净流入' },
      both_available_days: 2,
      mean_abs_diff: 5e7,
      max_abs_diff: 1e8,
      note: '两端定义不同, 这是口径差异不是误差; 不取平均、不互相校准。',
    },
    {
      left: { source: 'thsdk_l2', field: '主力净流入' },
      right: { source: 'tencent_dark', field: '主力净额（≥20万）' },
      both_available_days: 0,
      mean_abs_diff: null,
      max_abs_diff: null,
      note: '两端定义不同, 这是口径差异不是误差; 不取平均、不互相校准。',
    },
  ],
  archived_days: 2,
  note: '',
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('口径漂移区(B5b)', () => {
  it('每源一列各自显示原值, 不出现"平均成 1.5亿"的合成值', async () => {
    compareMock.mockResolvedValue(COMPARE)
    driftMock.mockResolvedValue(DRIFT_WITH_DATA)
    render(<CaliberComparePage />)
    // 等**漂移数据**就绪(只等静态标题会有竞态)
    await waitFor(() => expect(screen.getByText('2026-09-17')).toBeTruthy())

    // 三个源的值原样并排: +1.00亿 / +1.50亿 / +2.00亿
    // (1.00亿 两天都有 → getAllByText; 关键是三源各自的**原值**都在, 而不是被平均成 1.5亿)
    // 1.00亿: 漂移区 2 天各一次 + 对照区 1 次 = 3
    expect(screen.getAllByText('+1.00亿').length).toBe(3)
    expect(screen.getAllByText('+1.50亿').length).toBe(1)
    expect(screen.getAllByText('+2.00亿').length).toBe(1)
    // 负值照实显示(涨红跌绿的绿)
    expect(screen.getAllByText('-5000.00万').length).toBe(1)
  })

  it('没留痕的日期显示「该日未留痕」而不是 0', async () => {
    compareMock.mockResolvedValue(COMPARE)
    driftMock.mockResolvedValue(DRIFT_WITH_DATA)
    render(<CaliberComparePage />)
    await waitFor(() => expect(screen.getByText('2026-09-16')).toBeTruthy())
    const stale = screen.getAllByText('该日未留痕')
    expect(stale.length).toBeGreaterThan(0)
    // 该单元格不能渲染成 0 / +0.00
    expect(screen.queryByText('+0.00')).toBeNull()
  })

  it('空留痕时说明采集时间, 并明说不会用 0/推算值填补', async () => {
    compareMock.mockResolvedValue(COMPARE)
    driftMock.mockResolvedValue({
      symbol: '002361',
      days: 30,
      field_by_source: FIELDS,
      series: [],
      comparisons: [],
      archived_days: 0,
      note: '',
    })
    render(<CaliberComparePage />)
    await waitFor(() => expect(screen.getByText(/暂无留痕/)).toBeTruthy(), { timeout: 3000 })
    // 这段文案由多个元素拼成 → 断言容器文本, 并用 waitFor 消除竞态(避免脆弱的元素级匹配)
    await waitFor(
      () => {
        const body = document.body.textContent || ''
        expect(body).toContain('交易日 15:55 自动采集')
        expect(body).toContain('不会用 0 或推算值填补')
      },
      { timeout: 3000 },
    )
  })

  it('跨源差异写明比的是哪两个字段, 并标注"口径差异不是误差"', async () => {
    compareMock.mockResolvedValue(COMPARE)
    driftMock.mockResolvedValue(DRIFT_WITH_DATA)
    render(<CaliberComparePage />)
    await waitFor(() => expect(screen.getByText('2026-09-17')).toBeTruthy())
    // 两条比较都含同一个左源(明盘 L2「主力净流入」) → 用 getAllByText
    await waitFor(() => expect(screen.getAllByText(/明盘 L2「主力净流入」/).length).toBe(2), {
      timeout: 3000,
    })
    expect(screen.getByText(/东财四档「主力净流入」/)).toBeTruthy()
    expect(screen.getAllByText(/两端都有 2 天/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/口径差异不是误差/).length).toBeGreaterThan(0)
  })

  it('样本不足(0 天)时不给统计数字, 只说样本不足', async () => {
    compareMock.mockResolvedValue(COMPARE)
    driftMock.mockResolvedValue(DRIFT_WITH_DATA)
    render(<CaliberComparePage />)
    await waitFor(() => expect(screen.getByText(/两端都有 0 天/)).toBeTruthy(), { timeout: 3000 })
    expect(screen.getByText(/样本不足，暂不给差异统计/)).toBeTruthy()
  })
})
