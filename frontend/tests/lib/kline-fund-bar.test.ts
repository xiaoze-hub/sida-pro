// Task 19 回归: L3 资金柱的明盘字段名。
//
// 真缺陷(预置, 非本任务引入): `KlineChart`/`InteractiveKline` 的 `FundFlowBar` 声明 `open_net`,
// 渲染处也只读 `open_net` —— 但后端 `/klines/{s}/summary`.fund_flow 的真实字段是 **`ming_net`**
// (见 `src/web/api/klines.py:570/589`), 于是明盘分量恒 0, 资金柱只剩暗盘。
// 修法: 渲染改走本文件的纯函数 `fundBarPoint`(优先 `ming_net`, `open_net` 仅兜底旧调用方)。
//
// 本文件钉住三件事:
//  ① `ming_net` 被读入(净额含明盘分量)—— 这是修复的核心断言;
//  ② `open_net` 仍被兜底(不打断既有调用方);
//  ③ 明盘/暗盘/两者皆无 的分色规则。
import { describe, expect, it } from 'vitest'
import { capitalBarRows, fundBarPoint } from '@panwatch/biz-ui/components/KlineChart'
import type { StockColors } from '@panwatch/biz-ui/lib/stock-colors'

const SC: StockColors = { up: '#ef4444', down: '#22c55e' }
const NODATA = '#888888'
const call = (bar: { ming_net?: number | null; open_net?: number | null; dark_net?: number | null }) =>
  fundBarPoint({ date: '2026-09-11', ...bar }, '1d', SC, NODATA)

describe('fundBarPoint (L3 资金柱: 明盘字段名 ming_net)', () => {
  it('读 `ming_net`(后端真实字段): 净额 = 明盘 + 暗盘', () => {
    const p = call({ ming_net: 1_000_000, dark_net: 2_000_000 })
    expect(p.value).toBe(3_000_000)
    // 有暗盘 → 暗盘色(实心 up 令牌原文)
    expect(p.color).toBe(SC.up)
  })

  it('**只给 ming_net**(历史逐日明盘为 null, 当日有值)→ 净额 = 明盘, 不再恒 0', () => {
    const p = call({ ming_net: -5_000_000, dark_net: null })
    expect(p.value).toBe(-5_000_000) // 修复前这里是 0(明盘被吞)
    expect(p.color).toBe('rgba(34, 197, 94, 0.55)') // 明盘色 = down + 55% 透明
  })

  it('`open_net` 仍作旧调用方兜底(不打断既有接线)', () => {
    const p = call({ open_net: 700_000, dark_net: null })
    expect(p.value).toBe(700_000)
  })

  it('`ming_net` 优先于 `open_net`(同时给时以真实字段为准)', () => {
    const p = call({ ming_net: 111, open_net: 999, dark_net: null })
    expect(p.value).toBe(111)
  })

  it('两者皆无 → value 0 + nodata 中性色(不编造涨跌)', () => {
    const p = call({ ming_net: null, open_net: null, dark_net: null })
    expect(p.value).toBe(0)
    expect(p.color).toBe(NODATA)
  })

  it('脏值(NaN/字符串)按缺失处理, 不污染净额', () => {
    const p = call({ ming_net: Number.NaN, open_net: '123' as never, dark_net: undefined })
    expect(p.value).toBe(0)
    expect(p.color).toBe(NODATA)
  })

  it('时间戳按日线口径(该日 00:00Z 秒数)', () => {
    const p = call({ ming_net: 1, dark_net: null })
    expect(p.time).toBe(Math.floor(new Date('2026-09-11T00:00:00Z').getTime() / 1000))
  })
})

/**
 * Finding 2: `InteractiveKline` 的 L3 资金柱逐日映射(`capitalBarRows`) —— 它现在与 `KlineChart`
 * **共用** `fundBarPoint` 的 net 定义。这里直接钉住 `InteractiveKline` 渲染 effect 实际调用的
 * 那个纯函数的**柱值**, 覆盖旧实现(`value = on ?? dn ?? 0`, 只明盘或只暗盘 + 无 NaN 守卫)会错的
 * 三种情形: **暗盘独有**、**明盘 + 暗盘相加**、**NaN 脏值**。
 */
describe('capitalBarRows (InteractiveKline L3 资金柱: 与 KlineChart 同 net 定义)', () => {
  const dates = ['2026-09-10', '2026-09-11', '2026-09-12']
  const rows = (bars: Array<{ date: string; ming_net?: number | null; open_net?: number | null; dark_net?: number | null }>) =>
    capitalBarRows(dates, bars, SC, NODATA)

  it('暗盘独有(明盘 null): 柱值 = 暗盘(旧实现 `on ?? dn ?? 0` 恰好也取暗盘, 但此处钉住不回归)', () => {
    const r = rows([{ date: '2026-09-11', ming_net: null, open_net: null, dark_net: -400_000 }])
    expect(r[1]).toEqual({ date: '2026-09-11', value: -400_000, color: SC.down })
  })

  it('明盘 + 暗盘**相加**(旧实现 `on ?? dn ?? 0` 会丢暗盘, 只给明盘 ⇒ 值偏小)', () => {
    const r = rows([{ date: '2026-09-11', ming_net: 1_000_000, dark_net: 2_000_000 }])
    expect(r[1]?.value).toBe(3_000_000)
  })

  it('NaN 脏值被 `numOrNull` 挡住(旧实现 `typeof NaN === "number"` ⇒ NaN 混入柱值)', () => {
    const r = rows([{ date: '2026-09-11', ming_net: Number.NaN, dark_net: Number.NaN }])
    expect(r[1]).toEqual({ date: '2026-09-11', value: 0, color: NODATA })
    // 明确断言不是 NaN(否则 lightweight-charts 会因 NaN 柱高崩/画空)
    expect(Number.isNaN(r[1]?.value)).toBe(false)
  })

  it('逐根对齐 K 线日期: 无资金数据的那根为 null(调用方过滤)', () => {
    const r = rows([{ date: '2026-09-11', ming_net: 5, dark_net: null }])
    expect(r[0]).toBeNull()
    expect(r[1]?.value).toBe(5)
    expect(r[2]).toBeNull()
  })
})
