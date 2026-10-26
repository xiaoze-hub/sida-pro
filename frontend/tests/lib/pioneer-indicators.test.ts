// 决策先锋辅助指标(趋势操盘线/牛熊线/分时突破)前端纯逻辑单测(lib/pioneer-indicators)。
//
// 真行为测试(纯函数, 无 DOM/canvas), 钉死:
//  ① 白名单清洗: 脏点(缺字段/非法枚举/非有限数)不进图;
//  ② 三线序列与后端同源公式(EMA 首值播种 / SMA 窗口 / WMA 线性权重), 且周期**取 API params**不硬编码;
//  ③ 买卖点 marker 买红卖绿(买=下方 arrowUp / 卖=上方 arrowDown), 无合法日期不画;
//  ④ 牛熊线 B/S 由 signal + cross.time 定位;
//  ⑤ 降级文案: 缺逐分钟 DDE 时给统一文案。
import { describe, expect, it } from 'vitest'

import {
  MINUTE_DDE_DEGRADE_TEXT,
  emaSeries,
  minuteDegradeText,
  niuxiongSeries,
  niuxiongSignalMarkers,
  normalizeMinuteBreakthrough,
  normalizeNiuxiong,
  normalizeTrendLine,
  smaSeries,
  trendLineSeries,
  trendSignalMarkers,
  wmaSeries,
} from '@panwatch/biz-ui/lib/pioneer-indicators'

const COLORS = { buy: '#red', sell: '#green' }
const toTime = (d: string) => d

describe('normalizeTrendLine 白名单清洗', () => {
  it('保留合法三线 + 买卖点, 丢弃缺字段/非法项', () => {
    const out = normalizeTrendLine({
      available: true,
      lines: { red: 10.5, yellow: 9.8, green: 8.2 },
      band: { state: '多方带', low: 9.8, high: 10.5 },
      trend: '升势',
      buy_points: [
        { signal: 'buy', rule: 'pullback_green_yang', trigger: '回踩绿线收阳', price: 10.2, time: '2026-03-07' },
        'nope',
        null,
      ],
      sell_points: [],
      params: { red_period: 10, yellow_period: 20, green_period: 60, junk: 'x' },
      calibration: '逆向近似待校准',
    })!
    expect(out.lines).toEqual({ red: 10.5, yellow: 9.8, green: 8.2 })
    expect(out.band?.state).toBe('多方带')
    expect(out.buyPoints).toHaveLength(1)
    expect(out.buyPoints[0].signal).toBe('buy')
    expect(out.params).toEqual({ red_period: 10, yellow_period: 20, green_period: 60 }) // junk 非数被丢
  })

  it('三线不全(缺一根) → lines=null(不半画)', () => {
    const out = normalizeTrendLine({ available: false, lines: { red: 10, yellow: 9 } })!
    expect(out.lines).toBeNull()
    expect(out.available).toBe(false)
  })

  it('非对象输入 → null', () => {
    expect(normalizeTrendLine(undefined)).toBeNull()
    expect(normalizeTrendLine([])).toBeNull()
  })
})

describe('normalizeNiuxiong', () => {
  it('金叉 B 保留 + cross 归一(camel barsAgo)', () => {
    const out = normalizeNiuxiong({
      available: true,
      lines: { bull: 11, horse: 10, trade: 9.5 },
      signal: 'B',
      color: 'red',
      state: '牛线上方',
      cross: { type: 'golden', direction: 'B', bars_ago: 2, time: '2026-03-06' },
    })!
    expect(out.signal).toBe('B')
    expect(out.cross?.type).toBe('golden')
    expect(out.cross?.barsAgo).toBe(2)
    expect(out.cross?.time).toBe('2026-03-06')
  })

  it('非法 signal/color 归一为 null', () => {
    const out = normalizeNiuxiong({ available: true, signal: 'X', color: 'yellow' })!
    expect(out.signal).toBeNull()
    expect(out.color).toBeNull()
  })
})

describe('normalizeMinuteBreakthrough', () => {
  it('降级(available=false)保留 reasons + degraded', () => {
    const out = normalizeMinuteBreakthrough({
      available: false,
      degraded: true,
      reasons: ['DDE大单流入序列缺失'],
      signal_type: null,
    })!
    expect(out.available).toBe(false)
    expect(out.degraded).toBe(true)
    expect(out.reasons).toEqual(['DDE大单流入序列缺失'])
    expect(out.signalType).toBeNull()
  })

  it('「突」信号 + 条件清单', () => {
    const out = normalizeMinuteBreakthrough({
      available: true,
      signal_type: '突',
      trigger_time: '10:12',
      conditions: [{ name: '盘整>15分钟', met: true, detail: '振幅0.3%' }],
      met_conditions: ['盘整>15分钟'],
    })!
    expect(out.signalType).toBe('突')
    expect(out.triggerTime).toBe('10:12')
    expect(out.conditions).toHaveLength(1)
    expect(out.metConditions).toEqual(['盘整>15分钟'])
  })
})

describe('三线序列(与后端同源公式, 周期取 params)', () => {
  it('emaSeries 首值播种', () => {
    const out = emaSeries([1, 2, 3, 4, 5], 3)
    expect(out[0]).toBe(1)
    expect(out[4]).toBeCloseTo(4.0625, 6)
  })

  it('smaSeries 前 period-1 位为 null', () => {
    const out = smaSeries([1, 2, 3, 4, 5], 3)
    expect(out.slice(0, 2)).toEqual([null, null])
    expect(out[2]).toBe(2)
    expect(out[4]).toBe(4)
  })

  it('wmaSeries 线性权重(最近值最大)', () => {
    const out = wmaSeries([1, 2, 3, 4, 5], 3)
    expect(out[0]).toBeNull()
    expect(out[2]).toBeCloseTo(14 / 6, 6) // (1*1+2*2+3*3)/6
    expect(out[4]).toBeCloseTo(26 / 6, 6) // (1*3+2*4+3*5)/6
  })

  it('trendLineSeries 周期**取 params**(改参数 → 序列变, 不写死)', () => {
    const closes = [1, 2, 3, 4, 5, 6]
    const a = trendLineSeries(closes, { red_period: 2, yellow_period: 3, green_period: 4 })
    const b = trendLineSeries(closes, { red_period: 5, yellow_period: 3, green_period: 4 })
    expect(a.red).not.toEqual(b.red) // red 周期变了 → 红序列变
    expect(a.yellow).toEqual(b.yellow) // yellow 周期没变 → 黄序列不变
    expect(a.red).toEqual(emaSeries(closes, 2))
  })

  it('niuxiongSeries: 牛=WMA / 马=SMA / 买卖线=SMA(取 params)', () => {
    const closes = [10, 11, 12, 13, 14, 15]
    const s = niuxiongSeries(closes, { bull_period: 3, horse_period: 2, trade_period: 3 })
    expect(s.bull).toEqual(wmaSeries(closes, 3))
    expect(s.horse).toEqual(smaSeries(closes, 2))
    expect(s.trade).toEqual(smaSeries(closes, 3))
  })
})

describe('买卖点 marker(买红卖绿)', () => {
  const trend = normalizeTrendLine({
    available: true,
    lines: { red: 1, yellow: 2, green: 3 },
    buy_points: [{ signal: 'buy', time: '2026-03-07', rule: 'r', trigger: 't' }],
    sell_points: [{ signal: 'sell', time: '2026-03-09', rule: 'r2', trigger: 't2' }],
  })!

  it('买=红/下方/arrowUp, 卖=绿/上方/arrowDown', () => {
    const ms = trendSignalMarkers(trend, toTime, COLORS)
    const buy = ms.find((m) => m.text === '买')!
    const sell = ms.find((m) => m.text === '卖')!
    expect(buy).toMatchObject({ position: 'belowBar', color: '#red', shape: 'arrowUp' })
    expect(sell).toMatchObject({ position: 'aboveBar', color: '#green', shape: 'arrowDown' })
  })

  it('输出按时间升序(图表库要求)', () => {
    const ms = trendSignalMarkers(trend, toTime, COLORS)
    expect(ms.map((m) => m.time)).toEqual(['2026-03-07', '2026-03-09'])
  })

  it('无合法日期的点不画(不给假定位)', () => {
    const dirty = normalizeTrendLine({
      available: true,
      lines: { red: 1, yellow: 2, green: 3 },
      buy_points: [{ signal: 'buy', time: 'bad' }, { signal: 'buy', time: null }],
    })!
    expect(trendSignalMarkers(dirty, toTime, COLORS)).toHaveLength(0)
  })

  it('牛熊线 B/S 由 cross.time 定位; 无 cross.time 不画', () => {
    const nx = normalizeNiuxiong({
      available: true,
      lines: { bull: 1, horse: 2, trade: 3 },
      signal: 'B',
      cross: { type: 'golden', direction: 'B', bars_ago: 1, time: '2026-03-06' },
    })!
    const ms = niuxiongSignalMarkers(nx, toTime, COLORS)
    expect(ms).toHaveLength(1)
    expect(ms[0]).toMatchObject({ time: '2026-03-06', position: 'belowBar', color: '#red', text: 'B' })

    const noCross = normalizeNiuxiong({ available: true, signal: 'B', cross: { type: 'golden', direction: 'B', time: null } })!
    expect(niuxiongSignalMarkers(noCross, toTime, COLORS)).toHaveLength(0)
  })
})

describe('降级文案', () => {
  it('缺逐分钟 DDE(reasons 为空或含 DDE)→ 统一降级文案', () => {
    expect(minuteDegradeText([])).toBe(MINUTE_DDE_DEGRADE_TEXT)
    expect(minuteDegradeText(['DDE大单流入序列缺失'])).toBe(MINUTE_DDE_DEGRADE_TEXT)
  })
  it('其它原因原样透传(不替换成泛化文案)', () => {
    expect(minuteDegradeText(['分钟数据缺失'])).toBe('暂不出信号')
  })
})
