// K 线主图 GS 买卖点/区间色带 + 暗盘资金副图 —— **源码侧契约**(2026-10-11)。
//
// 为什么不全靠渲染测: marker(▲/▼)与色带/暗盘柱画在 canvas 上, DOM 里查不到; 而
// "日线口径门控 / 买红卖绿形状 / 区间只用已确认信号 / L1_approx 诚实标注"这些接线一旦被
// 日后重构悄悄删掉, 渲染测未必红。这里按行钉住关键接线(与 kline-pattern-contract.test.ts 同思路)。
import { readFileSync } from 'fs'
import { resolve } from 'path'

import { describe, expect, it } from 'vitest'

const KC = readFileSync(
  resolve(__dirname, '../../packages/biz-ui/src/components/KlineChart.tsx'),
  'utf-8',
)

describe('KlineChart 主图 GS 买卖点 + 区间色带契约(规格 §1)', () => {
  it('GS 买卖点用 ▲/▼ 形状(G 红▲在下 / S 绿▼在上), 非旧 circle', () => {
    expect(KC).toMatch(/shape: isBuy \? \('arrowUp' as const\) : \('arrowDown' as const\)/)
    expect(KC).toMatch(/position: isBuy \? \('belowBar' as const\) : \('aboveBar' as const\)/)
    expect(KC).toMatch(/color: gsColorFor\(g\.side, gs\)/)
    // 旧 circle 形状不得复活
    expect(KC).not.toMatch(/shape: 'circle' as const,\s*\n\s*size: g\.confirmed/)
  })

  it('GS 买卖点受**日线口径门控**(与 trendLine/niuxiong 同约定)', () => {
    expect(KC).toMatch(/if \(showSignal && interval === '1d'\) \{/)
  })

  it('区间色带: 主图独立隐藏轴 + gsZonePoints 纯函数 + 逐根 setData', () => {
    expect(KC).toMatch(/const GSZONE_SCALE = 'gszone'/)
    expect(KC).toMatch(/gsZonePoints\(effGsSignals, rangeBarsRef\.current\.map\(\(b\) => b\.date\)\)/)
    expect(KC).toMatch(/color: GS_ZONE_COLOR\[z\.zone\]/)
    // 门控: 整层 signal 关 / 非日线 / 无信号 → 清空
    expect(KC).toMatch(/if \(showSignal && interval === '1d' && effGsSignals\.length > 0\)/)
  })

  it('区间图例存在且与买卖点图例共存(独立 testid)', () => {
    expect(KC).toMatch(/data-testid="gs-zone-legend"/)
    expect(KC).toMatch(/data-testid="gs-legend"/)
  })
})

describe('KlineChart 暗盘资金副图契约(规格 §2)', () => {
  it('副图 tab 加「暗盘资金」档', () => {
    expect(KC).toMatch(/KlineSubchart = 'vol' \| 'macd' \| 'active_ratio' \| 'phase' \| 'activity' \| 'dark_pool'/)
    expect(KC).toMatch(/\['dark_pool', '暗盘资金'\]/)
  })

  it('自取 /klines/{symbol}/pool-flow-series(懒取数: 仅切到该档才发)', () => {
    expect(KC).toMatch(/\/klines\/\$\{encodeURIComponent\(props\.symbol\)\}\/pool-flow-series\?market=CN&days=60/)
    expect(KC).toMatch(/subchart === 'dark_pool'/)
    expect(KC).toMatch(/timeoutMs: SUMMARY_TIMEOUT_MS/)
  })

  it('红绿柱经 darkPoolBarPoint 纯函数(0/脏值不画), 挂副图 pane', () => {
    expect(KC).toMatch(/darkPoolBarPoint\(p, interval, sc\)/)
    expect(KC).toMatch(/const DARKPOOL_SCALE = 'darkpool'/)
    expect(KC).toMatch(/darkPoolSeriesRef\.current/)
    // 暗盘档时成交量让位(免重叠)
    expect(KC).toMatch(/else if \(subchart === 'dark_pool'\) \{/)
  })

  it('图例显式标 L1_approx(诚实近似) + 四组合用法 + 非投资建议', () => {
    expect(KC).toMatch(/data-testid="dark-pool-legend"/)
    expect(KC).toMatch(/\{DARK_POOL_CONFIDENCE\}/)
    expect(KC).toMatch(/涨\+红持股 \/ 跌\+绿回避 \/ 跌\+红吸筹 \/ 涨\+绿诱多/)
    expect(KC).toMatch(/非投资建议/)
  })

  it('悬停读数带出暗盘资金净额 + 四组合(darkPoolCombo)', () => {
    expect(KC).toMatch(/const hoverPoolCombo =/)
    expect(KC).toMatch(/darkPoolCombo\(hoverReadout\.poolChg \?\? null, hoverReadout\.poolNet \?\? null\)/)
  })
})

describe('活跃度副图仍为三色柱 + 1.56/3/6 阈值线(规格 §3, 核对未缺失)', () => {
  it('三色柱经 activityLevelColor + 三阈值线', () => {
    expect(KC).toMatch(/color: activityLevelColor\(p\.level\)/)
    expect(KC).toMatch(/\[\[1\.56, '生命1\.56'\], \[3, '强势3'\], \[6, '大牛6'\]\]/)
  })
})
