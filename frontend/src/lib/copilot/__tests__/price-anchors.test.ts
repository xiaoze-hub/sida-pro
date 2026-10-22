// @vitest-environment node
/**
 * 价格锚定纯函数回归（2026-10-23）。
 *
 * 收/拒表（每一条都对应真实参谋回答里出现过的写法）：
 *  收：`35.80` / `35.80元` / `站上 35.8` / `支撑 32.80` / `阻力 36.5` / `止损位 33.20` / `目标位 40`
 *  拒：`-1.9%`（pct，不是价位）/ `600111`（代码）/ `2026-10-22`（日期）/ `552 万`（数量）
 *
 * 另外钉两个诚信点：
 *  - `anchorKindLabel` 判不出上下文返回 `--`，绝不默认「支撑」；
 *  - 同一句里两个价位，标签按**离得最近**的价位词判（支撑 32.80 阻力 36.5 不能都标支撑）。
 */
import { describe, expect, it } from 'vitest'

import {
  anchorKindLabel,
  extractPriceAnchors,
  isAnchorClickable,
  splitByAnchors,
} from '@/lib/copilot/price-anchors'

const values = (text: string) => extractPriceAnchors(text).map((a) => a.value)

describe('extractPriceAnchors — 收', () => {
  it('裸小数价 35.80 → price', () => {
    const [a] = extractPriceAnchors('当前 35.80')
    expect(a).toMatchObject({ raw: '35.80', value: 35.8, kind: 'price' })
  })

  it('带单位 35.80元 → price，raw 含「元」', () => {
    const [a] = extractPriceAnchors('收在 35.80元')
    expect(a).toMatchObject({ raw: '35.80元', value: 35.8, kind: 'price' })
    expect(a.end - a.start).toBe('35.80元'.length)
  })

  it('站上 35.8 → level，标签「价位」', () => {
    const [a] = extractPriceAnchors('站上 35.8')
    expect(a).toMatchObject({ value: 35.8, kind: 'level' })
    expect(anchorKindLabel(a.kind, a.context)).toBe('价位')
  })

  it('支撑 32.80 → level，标签「支撑」', () => {
    const [a] = extractPriceAnchors('下方支撑 32.80')
    expect(a).toMatchObject({ value: 32.8, kind: 'level' })
    expect(a.context).toContain('支撑')
    expect(anchorKindLabel(a.kind, a.context)).toBe('支撑')
  })

  it('阻力 36.5 → level，标签「阻力」', () => {
    const [a] = extractPriceAnchors('上方阻力 36.5')
    expect(anchorKindLabel(a.kind, a.context)).toBe('阻力')
  })

  it('止损位 33.20 → level，标签「止损」（不是「价位」）', () => {
    const [a] = extractPriceAnchors('止损位 33.20')
    expect(a).toMatchObject({ value: 33.2, kind: 'level' })
    expect(anchorKindLabel(a.kind, a.context)).toBe('止损')
  })

  it('整数价位只在带价位词时才算：目标位 40', () => {
    const [a] = extractPriceAnchors('目标位 40')
    expect(a).toMatchObject({ value: 40, kind: 'level' })
    expect(anchorKindLabel(a.kind, a.context)).toBe('目标')
  })

  it('货币符号后的整数也算：¥ 12', () => {
    expect(values('成本 ¥ 12')).toEqual([12])
  })

  it('一句话里两个价位，标签按最近的价位词判', () => {
    const anchors = extractPriceAnchors('支撑 32.80，阻力 36.5')
    expect(anchors.map((a) => a.value)).toEqual([32.8, 36.5])
    expect(anchorKindLabel(anchors[0].kind, anchors[0].context)).toBe('支撑')
    expect(anchorKindLabel(anchors[1].kind, anchors[1].context)).toBe('阻力')
  })

  it('日期在句首时，后面的价位照常抽出（遮罩不串位）', () => {
    const anchors = extractPriceAnchors('2026-10-22 收盘 35.80')
    expect(anchors.map((a) => a.raw)).toEqual(['35.80'])
    expect(anchors[0].start).toBe('2026-10-22 收盘 '.length)
  })
})

describe('extractPriceAnchors — 拒', () => {
  it('百分比不是价位：-1.9% → kind pct，且不可点', () => {
    const [a] = extractPriceAnchors('今日 -1.9%')
    expect(a.kind).toBe('pct')
    expect(a.value).toBeCloseTo(-1.9)
    expect(isAnchorClickable(a)).toBe(false)
  })

  it('6 位代码不是价位：600111', () => {
    expect(extractPriceAnchors('600111 涨停')).toEqual([])
    expect(extractPriceAnchors('代码 000001')).toEqual([])
  })

  it('日期不是价位：2026-10-22 / 2026年10月22日', () => {
    expect(extractPriceAnchors('2026-10-22')).toEqual([])
    expect(extractPriceAnchors('2026年10月22日')).toEqual([])
  })

  it('数量词不是价位：552 万 / 3 条 / 20 日', () => {
    expect(extractPriceAnchors('主力净流出 552 万元')).toEqual([])
    expect(extractPriceAnchors('3 条依据')).toEqual([])
    expect(extractPriceAnchors('持股 20 日')).toEqual([])
  })

  it('量纲外的数字丢弃（成交额 12345）', () => {
    expect(extractPriceAnchors('成交额 12345')).toEqual([])
  })
})

describe('anchorKindLabel — 不猜', () => {
  it('上下文为空：level → `--`（绝不默认支撑）', () => {
    expect(anchorKindLabel('level', '')).toBe('--')
    expect(anchorKindLabel('level', '附近有')).toBe('--')
  })

  it('纯裸价 → 价位（中性类别，不是猜测）', () => {
    expect(anchorKindLabel('price', '')).toBe('价位')
  })

  it('pct 一律 --', () => {
    expect(anchorKindLabel('pct', '支撑')).toBe('--')
  })

  it('压力 归一成 阻力', () => {
    expect(anchorKindLabel('level', '上方压力')).toBe('阻力')
  })
})

describe('splitByAnchors', () => {
  it('切片拼回去与原文逐字相等', () => {
    const text = '下方支撑 32.80，上方阻力 36.5，止损位 33.20'
    const segs = splitByAnchors(text)
    expect(segs.map((s) => s.text).join('')).toBe(text)
    expect(segs.filter((s) => s.anchor).map((s) => s.anchor!.value)).toEqual([32.8, 36.5, 33.2])
  })

  it('没有价位时单段返回', () => {
    expect(splitByAnchors('没有数字的句子')).toEqual([{ text: '没有数字的句子' }])
  })

  it('百分比不会被切成可点片段', () => {
    const segs = splitByAnchors('下跌 -1.9%')
    expect(segs).toEqual([{ text: '下跌 -1.9%' }])
  })
})
