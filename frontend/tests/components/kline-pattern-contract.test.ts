// K线形态图层 —— **源码侧契约**(2026-10-10)。
//
// 为什么不全部靠渲染测: 形态记号画在 canvas 上(marker), DOM 里查不到; 且"买红卖绿/箭头方向/门控"
// 这些接线一旦被日后重构悄悄删掉, 渲染测未必红。这里按行钉住关键接线, 与
// `kline-cost-line-contract.test.ts` 同思路。
import { readFileSync } from 'fs'
import { resolve } from 'path'

import { describe, expect, it } from 'vitest'

const KC = readFileSync(
  resolve(__dirname, '../../packages/biz-ui/src/components/KlineChart.tsx'),
  'utf-8',
)

describe('KlineChart K线形态图层契约', () => {
  it('自取 /klines/{symbol}/patterns(与主图同源同口径, 走 encodeURIComponent)', () => {
    expect(KC).toMatch(/\/klines\/\$\{encodeURIComponent\(props\.symbol\)\}\/patterns\?market=/)
  })

  it('受 layersVisible.pattern 门控(整层关既不取也不画)', () => {
    expect(KC).toMatch(/props\.layersVisible\?\.pattern !== false/)
  })

  it('父传 patterns 则不重复自取(ownPatterns)', () => {
    expect(KC).toMatch(/const ownPatterns = props\.patterns === undefined/)
    expect(KC).toMatch(/props\.patterns \?\? patternMarks \?\? EMPTY_PATTERNS/)
  })

  it('形态记号经 buildPatternMarkers 纯函数生成(买红卖绿取读色令牌, 不硬编码)', () => {
    expect(KC).toMatch(/buildPatternMarkers\(effPatterns/)
    expect(KC).toMatch(/up: pc\.up/)
    expect(KC).toMatch(/down: pc\.down/)
    expect(KC).toMatch(/readStockColors\(\)/)
  })

  it('悬停读数带出形态(证据描述)', () => {
    expect(KC).toMatch(/patternHoverLabels\(patternsRef\.current, hitDate\)/)
    expect(KC).toMatch(/形态 \{hoverReadout\.patterns\.join/)
  })

  it('图例存在且明确"客观标注 · 非投资建议"', () => {
    expect(KC).toMatch(/data-testid="pattern-legend"/)
    expect(KC).toMatch(/客观标注 · 非投资建议/)
  })

  it('取数失败 → 空数组显式降级(不编造), 且带显式超时', () => {
    expect(KC).toMatch(/setPatternMarks\(normalizePatterns\(res\?\.patterns\)\)/)
    expect(KC).toMatch(/setPatternMarks\(\[\]\)/)
    expect(KC).toMatch(/timeoutMs: SUMMARY_TIMEOUT_MS/)
  })
})
