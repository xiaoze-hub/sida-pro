// K线形态标注纯逻辑单测(lib/kline-patterns): 白名单清洗 + 记号映射 + 悬停读数。
//
// 这些是**真行为**测试(纯函数, 无 DOM/canvas), 钉死:
//  ① 脏点不进图(缺 name/direction/index 一律丢);
//  ② 买红卖绿: 看涨=红/下方/arrowUp, 看跌=绿/上方/arrowDown;
//  ③ 同 (交易日+方向) 的多形态合并为一个记号(不重合);
//  ④ 无合法日期的形态不画(不给假定位);
//  ⑤ 输出按时间升序(图表库要求);
//  ⑥ 悬停读数只描述形态, 不含买卖建议。
import { describe, expect, it } from 'vitest'

import {
  PATTERN_DIRECTION_LABEL,
  buildPatternMarkers,
  normalizePatterns,
  patternHoverLabels,
  type PatternMark,
} from '@panwatch/biz-ui/lib/kline-patterns'

const COLORS = { up: '#red', down: '#green', neutral: '#gray' }
const toTime = (d: string) => d

describe('normalizePatterns 白名单清洗', () => {
  it('保留合法项, 丢弃缺字段/非法枚举/非有限索引', () => {
    const raw = [
      { name: '红三兵', direction: 'bullish', index: 3, date: '2026-03-07', basis: ['a', 1] },
      { name: '', direction: 'bullish', index: 1 }, // 空名
      { name: 'X', direction: 'up', index: 1 }, // 非法方向
      { name: 'Y', direction: 'bearish', index: NaN }, // 非有限索引
      null,
      'nope',
      { name: '黄昏之星', direction: 'bearish', index: 9, date: null },
    ]
    const out = normalizePatterns(raw)
    expect(out.map((m) => m.name)).toEqual(['红三兵', '黄昏之星'])
    expect(out[0].basis).toEqual(['a']) // 非字符串依据被过滤
    expect(out[1].date).toBeNull()
  })

  it('非数组输入 → 空数组(不抛)', () => {
    expect(normalizePatterns(undefined)).toEqual([])
    expect(normalizePatterns({})).toEqual([])
  })
})

describe('buildPatternMarkers 记号映射(买红卖绿)', () => {
  const bull: PatternMark = { name: '红三兵', direction: 'bullish', index: 3, date: '2026-03-07' }
  const bear: PatternMark = { name: '黄昏之星', direction: 'bearish', index: 9, date: '2026-03-09' }

  it('看涨 → 红/下方/arrowUp; 看跌 → 绿/上方/arrowDown', () => {
    const ms = buildPatternMarkers([bull, bear], toTime, COLORS)
    const b = ms.find((m) => m.text === '红三兵')!
    const s = ms.find((m) => m.text === '黄昏之星')!
    expect(b).toMatchObject({ position: 'belowBar', color: '#red', shape: 'arrowUp' })
    expect(s).toMatchObject({ position: 'aboveBar', color: '#green', shape: 'arrowDown' })
  })

  it('同 (交易日+方向) 多个形态合并为一个记号(文本用 / 连接)', () => {
    const ms = buildPatternMarkers(
      [bull, { ...bull, name: '上升三法' }],
      toTime,
      COLORS,
    )
    expect(ms).toHaveLength(1)
    expect(ms[0].text).toBe('红三兵/上升三法')
    // 不同方向同日 → 两个记号(一红一下 一绿一上)
    const two = buildPatternMarkers([bull, { ...bear, date: '2026-03-07' }], toTime, COLORS)
    expect(two).toHaveLength(2)
  })

  it('无合法日期的形态不画(不给假定位)', () => {
    const ms = buildPatternMarkers(
      [bull, { ...bull, name: 'X', date: null }, { ...bull, name: 'Y', date: 'bad' }],
      toTime,
      COLORS,
    )
    expect(ms).toHaveLength(1)
    expect(ms[0].text).toBe('红三兵')
  })

  it('输出按时间升序(图表库要求)', () => {
    const late: PatternMark = { name: '黄昏之星', direction: 'bearish', index: 9, date: '2026-03-09' }
    const early: PatternMark = { name: '红三兵', direction: 'bullish', index: 3, date: '2026-03-07' }
    const ms = buildPatternMarkers([late, early], toTime, COLORS)
    expect(ms.map((m) => m.time)).toEqual(['2026-03-07', '2026-03-09'])
  })

  it('中性方向 → 灰/下方(防御性, 不产生买入色)', () => {
    const m: PatternMark = { name: '十字', direction: 'neutral', index: 1, date: '2026-03-07' }
    expect(buildPatternMarkers([m], toTime, COLORS)[0]).toMatchObject({
      position: 'belowBar',
      color: '#gray',
      shape: 'arrowUp',
    })
  })
})

describe('patternHoverLabels 悬停读数', () => {
  it('按交易日命中, 输出 形态名(方向); 无建议字样', () => {
    const marks: PatternMark[] = [
      { name: '红三兵', direction: 'bullish', index: 3, date: '2026-03-07' },
      { name: '黄昏之星', direction: 'bearish', index: 9, date: '2026-03-09' },
    ]
    expect(patternHoverLabels(marks, '2026-03-07')).toEqual(['红三兵(看涨)'])
    expect(patternHoverLabels(marks, '2026-03-08')).toEqual([])
    expect(patternHoverLabels(marks, null)).toEqual([])
    const text = patternHoverLabels(marks, '2026-03-09').join(' ')
    for (const w of ['建议', '买入', '卖出', '推荐']) expect(text).not.toContain(w)
  })

  it('方向标签齐全', () => {
    expect(PATTERN_DIRECTION_LABEL).toEqual({ bullish: '看涨', bearish: '看跌', neutral: '中性' })
  })
})
