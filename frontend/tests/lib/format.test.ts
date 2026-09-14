import { describe, expect, it } from 'vitest'
import {
  safeNum,
  safeFixed,
  safePercent,
  safeMoney,
  safePrice,
  safeInt,
  safeNetInflow,
  safeMoneyUnsigned,
  toAmount,
  toAmountFromWan,
  toAmountFromWanUnsigned,
} from '../../src/lib/format'

// E3(2026-09-09) 前端门禁配套单测: format.ts 是全项目数值展示的统一口径层,
// 崩溃史(c.price.toFixed is not a function / NaN亿)见 aud-20260823 S-5/M-3~M-8。

describe('safeNum 基础护栏', () => {
  it('null/undefined/空串/NaN/非数值字符串 → null', () => {
    expect(safeNum(null)).toBeNull()
    expect(safeNum(undefined)).toBeNull()
    expect(safeNum('')).toBeNull()
    expect(safeNum('abc')).toBeNull()
    expect(safeNum(NaN)).toBeNull()
    expect(safeNum(Infinity)).toBeNull()
  })

  it('数字与字符串数字正常转换, 0 不误判', () => {
    expect(safeNum(3.5)).toBe(3.5)
    expect(safeNum('3.5')).toBe(3.5)
    expect(safeNum(0)).toBe(0)
  })
})

describe('safeFixed / safePercent 展示', () => {
  it('safeFixed 保留指定小数位, 无效值走 fallback', () => {
    expect(safeFixed(3.456)).toBe('3.46')
    expect(safeFixed('12.3', 1)).toBe('12.3')
    expect(safeFixed(null)).toBe('--')
    expect(safeFixed('x', 2, 'N/A')).toBe('N/A')
  })

  it('safePercent 带符号百分比(仅正数加号), 无效值 --', () => {
    expect(safePercent(3.45)).toBe('+3.45%')
    expect(safePercent(-1.2)).toBe('-1.20%')
    expect(safePercent(0)).toBe('0.00%')
    expect(safePercent(null)).toBe('--')
    expect(safePercent(5, 2, false)).toBe('5.00%')
  })
})

describe('safeMoney 金额口径(元 → 亿/万)', () => {
  it('≥1e8 转亿, ≥1e4 转万, 带正负号', () => {
    expect(safeMoney(1.5e8)).toBe('+1.50亿')
    expect(safeMoney(-2.5e8)).toBe('-2.50亿')
    expect(safeMoney(2.5e4)).toBe('+2.50万')
    expect(safeMoney(-30000)).toBe('-3.00万')
  })

  it('小金额保留精度并去尾零(资金口径正数也带 +)', () => {
    expect(safeMoney(123.45)).toBe('+123.45')
    expect(safeMoney(100.0)).toBe('+100')
    expect(safeMoney(0.5)).toBe('+0.5')
    expect(safeMoney(0.51234)).toBe('+0.5123')
  })

  it('无效值 fallback', () => {
    expect(safeMoney(null)).toBe('--')
    expect(safeMoney('abc')).toBe('--')
  })
})

// 2026-09-14 持仓页缺陷: 「可用资金/总资产/总市值」是**存量**读数, 带 '+' 会被读成变化量;
// 涨跌/盈亏仍要走 safeMoney 的带符号口径(红涨绿跌需要方向)。
describe('safeMoneyUnsigned 存量金额口径(元 → 亿/万, 不带 +)', () => {
  it('正数不带 +, 负号保留(负数是真读数, 不吞符号), 缺失 --', () => {
    expect(safeMoneyUnsigned(4.5e4)).toBe('4.50万')
    expect(safeMoneyUnsigned(1.5e8)).toBe('1.50亿')
    expect(safeMoneyUnsigned(-2.5e8)).toBe('-2.50亿')
    expect(safeMoneyUnsigned(-30000)).toBe('-3.00万')
    expect(safeMoneyUnsigned(null)).toBe('--')
    expect(safeMoneyUnsigned(undefined)).toBe('--')
    expect(safeMoneyUnsigned('abc')).toBe('--')
    expect(safeMoneyUnsigned(Number.NaN)).toBe('--')
  })

  it('0 渲染 "0"(与 safeMoney 一致, 不带号): 总市值 0 不会显示成 "+0"', () => {
    expect(safeMoneyUnsigned(0)).toBe('0')
    expect(safeMoney('0')).toBe('0')
  })

  it('小金额去尾零, 与 safeMoney 的量级规则同源', () => {
    expect(safeMoneyUnsigned(123.45)).toBe('123.45')
    expect(safeMoneyUnsigned(100.0)).toBe('100')
    expect(safeMoneyUnsigned(0.5)).toBe('0.5')
  })

  it('同值对照: safeMoney 带 +, safeMoneyUnsigned 不带(避免后人把两者合并)', () => {
    expect(safeMoney(4.5e4)).toBe('+4.50万')
    expect(safeMoneyUnsigned(4.5e4)).toBe('4.50万')
    expect(safeMoney(-1e4)).toBe('-1.00万')
    expect(safeMoneyUnsigned(-1e4)).toBe('-1.00万')
  })
})

describe('safePrice / safeInt / safeNetInflow', () => {
  it('safePrice 去尾零', () => {
    expect(safePrice(30.0)).toBe('30')
    expect(safePrice(30.5)).toBe('30.5')
    expect(safePrice(null)).toBe('--')
  })

  it('safeInt 千分位整数, 非数值 --', () => {
    expect(safeInt(1234567)).toBe('1,234,567')
    expect(safeInt('abc')).toBe('--')
  })

  it('safeNetInflow 带符号+亿单位', () => {
    expect(safeNetInflow(1.234)).toBe('+1.2亿')
    expect(safeNetInflow(-2.5)).toBe('-2.5亿')
    expect(safeNetInflow(null)).toBe('--')
  })
})

describe('toAmount 系列(元/万双口径)', () => {
  it('toAmount 元口径: 亿/万自动选, 符号恒显', () => {
    expect(toAmount(1.5e8)).toBe('+1.50亿')
    expect(toAmount(2.3e4)).toBe('+2.30万')
    expect(toAmount(-5e4)).toBe('-5.00万')
    expect(toAmount(null)).toBe('--')
    expect(toAmount(Number.NaN)).toBe('--')
  })

  it('toAmountFromWan 万口径: ≥1e4 万转亿', () => {
    expect(toAmountFromWan(15000)).toBe('+1.50亿')
    expect(toAmountFromWan(500)).toBe('+500.00万')
    expect(toAmountFromWan(-20000)).toBe('-2.00亿')
  })

  // 2026-09-14 暗盘 TOP 缺陷: 成交额是"规模量", 带 '+' 会被读成变化量。
  it('toAmountFromWanUnsigned 万口径无量值: 正数不带 +, 负号保留, 缺失 --', () => {
    expect(toAmountFromWanUnsigned(116836.13)).toBe('11.68亿')
    expect(toAmountFromWanUnsigned(15000)).toBe('1.50亿')
    expect(toAmountFromWanUnsigned(500)).toBe('500.00万')
    expect(toAmountFromWanUnsigned(0)).toBe('0.00万')
    expect(toAmountFromWanUnsigned(-500)).toBe('-500.00万')
    expect(toAmountFromWanUnsigned(null)).toBe('--')
    expect(toAmountFromWanUnsigned(Number.NaN)).toBe('--')
  })
})
