// 威科夫量价结构图层前端契约(本土三层滤网 P1)。
//
// 后端 `GET /klines/{symbol}/wyckoff` 下发与 patterns 同线型 markers,
// 前端**复用** patterns 管线(normalizePatterns/buildPatternMarkers/patternHoverLabels),
// 本文件钉死复用在 wyckoff 信号上的行为:
//  ① 后端线型(含 code 冗余键)可通过清洗(冗余键不破坏、不要求);
//  ② 配色语义: 放量滞涨(bearish)=绿/上方, 放量突破(bullish)=红/下方, 弹簧/沉淀(neutral)=灰/下方;
//  ③ 悬停读数只报结构名+方向, 不含买卖建议词;
//  ④ 开关语义由 KlineChart 层持有, 此处只验管线。
import { describe, expect, it } from 'vitest'

import {
  buildPatternMarkers,
  normalizePatterns,
  patternHoverLabels,
  type PatternMark,
} from '@sida/biz-ui/lib/kline-patterns'

const COLORS = { up: '#red', down: '#green', neutral: '#gray' }
const toTime = (d: string) => d

/** 后端 wyckoff 端点原样下发的 marker 形状(含 code 冗余键 + 中文展示名)。 */
const backendPayload = [
  {
    name: '弹簧试探',
    code: 'spring_probe',
    direction: 'neutral',
    category: '量价结构',
    index: 24,
    date: '2026-10-15',
    position: '低位',
    basis: ['盘中跌破近10日最低(34.00)', '收盘收回其上(35.14, 从最低回收3.5%)'],
    definition: '弹簧试探(待确认): …',
    span: [5, 24],
  },
  {
    name: '放量滞涨',
    code: 'effort_divergence',
    direction: 'bearish',
    category: '量价结构',
    index: 23,
    date: '2026-10-13',
    position: '高位',
    basis: ['量 1.9×20日均量(≥1.8×)', '涨幅 +0.80%(幅度<1.0%)'],
    definition: '放量滞涨: …',
    span: [4, 23],
  },
]

describe('wyckoff markers 复用 patterns 管线', () => {
  it('后端线型通过清洗: 保留 name/direction/index/date, code 冗余键不破坏', () => {
    const out = normalizePatterns(backendPayload)
    expect(out.map((m) => m.name)).toEqual(['弹簧试探', '放量滞涨'])
    expect(out[0]).toMatchObject({ direction: 'neutral', index: 24, date: '2026-10-15' })
  })

  it('配色语义: 滞涨绿/上方arrowDown, 弹簧灰/下方arrowUp', () => {
    const out = normalizePatterns(backendPayload)
    const ms = buildPatternMarkers(out, toTime, COLORS)
    const div = ms.find((m) => m.text === '放量滞涨')!
    const spr = ms.find((m) => m.text === '弹簧试探')!
    expect(div).toMatchObject({ position: 'aboveBar', color: '#green', shape: 'arrowDown' })
    expect(spr).toMatchObject({ position: 'belowBar', color: '#gray', shape: 'arrowUp' })
  })

  it('悬停读数: 结构名+方向, 无买卖建议词', () => {
    const out = normalizePatterns(backendPayload)
    const labels = patternHoverLabels(out, '2026-10-15')
    expect(labels).toEqual(['弹簧试探(中性)'])
    const blob = labels.join('')
    for (const w of ['买入', '卖出', '建仓', '主力', '加仓', '减仓']) {
      expect(blob).not.toContain(w)
    }
  })

  it('无合法日期的 marker 不画(不给假定位)', () => {
    const marks: PatternMark[] = [{ name: '缩量沉淀', direction: 'neutral', index: 9, date: null }]
    expect(buildPatternMarkers(marks, toTime, COLORS)).toEqual([])
  })
})
