// 图表说明(chart-notes)—— **源码侧契约**(2026-10-14 看图决策产品件)。
//
// 为什么不靠渲染测: 说明条/图例 title 的接线一旦被重构悄悄删掉, 渲染测未必红; 这里按行
// 钉住关键接线(与 kline-pattern-contract.test.ts 同思路)。
//
// 产品口径: 看图决策 = 提供图表说明供用户和 AI 共同决策 —— 图上每个标记都要能回答
// 「为什么在那」(触发/时点/失效), 且 AI 读的 = 图上标的(后端 chart-notes 单一事实源)。
// UI 铁律: 悬停浮层小字 / 图例扩展, **不卡片化、不加宽、不改 StockWorkbench 布局**。
import { readFileSync } from 'fs'
import { resolve } from 'path'

import { describe, expect, it } from 'vitest'

const KC = readFileSync(
  resolve(__dirname, '../../packages/biz-ui/src/components/KlineChart.tsx'),
  'utf-8',
)

describe('KlineChart 图表说明图层契约', () => {
  it('自取 /klines/{symbol}/chart-notes(与后端图层标记单一事实源)', () => {
    expect(KC).toMatch(
      /`\/klines\/\$\{encodeURIComponent\(props\.symbol\)\}\/chart-notes\?market=/,
    )
  })

  it('受 layersVisible.explain 门控(整层关既不取也不展示)', () => {
    expect(KC).toMatch(/const showExplainLayer = props\.layersVisible\?\.explain !== false/)
  })

  it('父传 chartNotes 则不重复自取(ownChartNotes)', () => {
    expect(KC).toMatch(/const ownChartNotes = props\.chartNotes === undefined/)
    expect(KC).toMatch(/props\.chartNotes \?\? chartNotesData\?\.notes \?\? EMPTY_NOTES/)
    expect(KC).toMatch(/props\.zoneNote \?\? chartNotesData\?\.zoneNote \?\? null/)
  })

  it('取不到显式降级(空说明, 不编造)', () => {
    expect(KC).toMatch(/setChartNotesData\(\{ notes: \[\], zoneNote: null \}\)/)
  })

  it('图下说明条存在(一行式小字 · 非卡片)', () => {
    expect(KC).toMatch(/data-testid="chart-notes-bar"/)
    expect(KC).toMatch(/notesBarText\(effChartNotes, 3\)/)
    // 非卡片: 复用 muted 小字, 不含 card/border 卡片容器
    expect(KC).not.toMatch(/data-testid="chart-notes-bar"[\s\S]{0,200}rounded-lg border/)
  })

  it('悬停读数带出「为什么」(触发/时点/失效)', () => {
    expect(KC).toMatch(/notesForDate\(chartNotesRef\.current, hitDate\)/)
    expect(KC).toMatch(/data-testid="chart-note-why"/)
    expect(KC).toMatch(/hoverReadout\.why/)
  })

  it('图例扩展: 形态/GS 图例项挂「为什么」title(与图上标的同源)', () => {
    expect(KC).toMatch(/noteForMarker\(effChartNotes, 'pattern', m\.date, m\.name\)/)
    expect(KC).toMatch(/data-testid="gs-legend-why"/)
    expect(KC).toMatch(/title=\{markerWhy\(effZoneNote\)\}/)
  })

  it('区说明与推送/战报同源(zoneNote 单点取句)', () => {
    expect(KC).toMatch(/zoneNoteText\(effZoneNote\)/)
  })

  it('说明的逻辑全部走 lib/chart-notes 纯函数(不内联重算口径)', () => {
    expect(KC).toMatch(/from '\.\.\/lib\/chart-notes'/)
    expect(KC).toMatch(/markerWhy/)
  })
})
