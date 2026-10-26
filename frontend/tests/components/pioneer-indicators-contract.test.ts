// 决策先锋辅助指标前端集成 —— **源码侧契约**(2026-10-10 P3 UI 集成)。
//
// 为什么不全部靠渲染测: 三线画在 canvas 上(LineSeries), marker 也在 canvas —— DOM 里查不到; 而
// "URL 接线 / 图层门控 / 用自己的 params 展开序列(不写死) / 买红卖绿取令牌 / 日线口径门控" 这些
// 一旦被日后重构悄悄删掉, 渲染测未必红。这里按行钉住关键接线(与 kline-pattern-contract 同思路)。
import { readFileSync } from 'fs'
import { resolve } from 'path'

import { describe, expect, it } from 'vitest'

const KC = readFileSync(resolve(__dirname, '../../packages/biz-ui/src/components/KlineChart.tsx'), 'utf-8')
const DC = readFileSync(resolve(__dirname, '../../packages/biz-ui/src/components/workbench/DecisionCard.tsx'), 'utf-8')
const SEC = readFileSync(resolve(__dirname, '../../packages/biz-ui/src/components/PioneerIndicatorsSection.tsx'), 'utf-8')

describe('KlineChart 决策先锋辅助指标图层契约', () => {
  it('自取 trend-line / niuxiong(走 encodeURIComponent, market 入参)', () => {
    expect(KC).toContain('/indicators/trend-line/${encodeURIComponent(props.symbol)}')
    expect(KC).toContain('/indicators/niuxiong/${encodeURIComponent(props.symbol)}')
    expect(KC).toContain('?market=${encodeURIComponent(props.market)}')
  })

  it('自取 minute-breakthrough(分时突破提示条)', () => {
    expect(KC).toContain('/indicators/minute-breakthrough/${encodeURIComponent(props.symbol)}')
  })

  it('受 layersVisible.{trendLine,niuxiong} 门控(整层关既不取也不画)', () => {
    expect(KC).toContain("props.layersVisible?.trendLine !== false")
    expect(KC).toContain("props.layersVisible?.niuxiong !== false")
  })

  it('父传 trendLine/niuxiong 则不重复自取(own*)', () => {
    expect(KC).toMatch(/const ownTrendLine = props\.trendLine === undefined/)
    expect(KC).toMatch(/const ownNiuxiong = props\.niuxiong === undefined/)
    expect(KC).toMatch(/const effTrendLine = props\.trendLine \?\? trendLineData/)
    expect(KC).toMatch(/const effNiuxiong = props\.niuxiong \?\? niuxiongData/)
  })

  it('三线序列用自己的 params 展开(不硬编码周期) + 仅日线口径画线', () => {
    expect(KC).toMatch(/trendLineSeries\(closes, effTrendLine\.params\)/)
    expect(KC).toMatch(/niuxiongSeries\(closes, effNiuxiong\.params\)/)
    expect(KC).toMatch(/interval !== '1d'/)
  })

  it('买卖点 marker 走纯函数 + 买红卖绿取读色令牌(不硬编码)', () => {
    expect(KC).toMatch(/trendSignalMarkers\(effTrendLine/)
    expect(KC).toMatch(/niuxiongSignalMarkers\(effNiuxiong/)
    expect(KC).toMatch(/const gcol = readGsColors\(\)/)
  })

  it('图例分组共存(trend-line-legend / niuxiong-legend) 且明确"客观标注 · 非投资建议"', () => {
    expect(KC).toContain('data-testid="trend-line-legend"')
    expect(KC).toContain('data-testid="niuxiong-legend"')
    expect(KC).toMatch(/PIONEER_NOT_ADVICE_TEXT/)
  })

  it('分时突破提示条存在 + 降级文案走纯函数(不编造信号)', () => {
    expect(KC).toContain('data-testid="minute-breakthrough-bar"')
    expect(KC).toMatch(/minuteDegradeText\(minuteBreakthrough\.reasons\)/)
    expect(KC).toMatch(/normalizeMinuteBreakthrough/)
  })
})

describe('DecisionCard 数据面板追加节契约', () => {
  it('引入并渲染 PioneerIndicatorsSection(追加节, 透传 symbol/market)', () => {
    expect(DC).toMatch(/import PioneerIndicatorsSection from '@panwatch\/biz-ui\/components\/PioneerIndicatorsSection'/)
    expect(DC).toMatch(/<PioneerIndicatorsSection symbol=\{symbol\} market=\{market\} \/>/)
  })
})

describe('PioneerIndicatorsSection 数据面板节契约', () => {
  it('自取 trend-line / niuxiong(与 K 线图层同源)', () => {
    expect(SEC).toContain('/indicators/trend-line/${encodeURIComponent(symbol)}')
    expect(SEC).toContain('/indicators/niuxiong/${encodeURIComponent(symbol)}')
  })

  it('仅 CN 标的取数', () => {
    expect(SEC).toMatch(/market\.toUpperCase\(\) !== 'CN'/)
  })

  it('数值格式化走 safe*(R6): 本文件零裸 .toFixed(', () => {
    expect((SEC.match(/\.toFixed\(/g) || []).length).toBe(0)
    expect(SEC).toMatch(/safeFixed/)
  })

  it('渲染校准标注 + 客观标注免责', () => {
    expect(SEC).toContain('data-testid="pioneer-calibration"')
    expect(SEC).toMatch(/PIONEER_NOT_ADVICE_TEXT/)
  })
})
