// @vitest-environment jsdom
/**
 * 纯时间点标注（2026-10-29）：服务端行 → 图表 marker 的**核对口径**。
 *
 * 这一层测的是"能不能画"的判断本身（不渲染图表）：
 *  - 只有日期没有价位的行**不该**被当成"没有图层的废行"丢掉，也不该被编一个价位；
 *  - 它走价位线**同一把尺子**：来源 ai / 属主是我 / 有本人确认动作 / 标的类型可证 /
 *    市场可证 / 周期逐字相等 —— 任何一条不满足都不画；
 *  - 带时分的 ts 不画（无法证明落在哪一根 K 线，不给假定位）；
 *  - 确认响应（apply）没有价位时的解析与 id 对上；
 *  - 两条通道互不冒充：有价位的行不会被当成时间点标注。
 */
import { describe, expect, it } from 'vitest'

import {
  marksForTarget,
  parseAnnotationsResponse,
  parseApplyResponse,
  verifyAppliedRow,
  verifyRow,
  verifyRowMarker,
  type AnnotationTarget,
} from '@/lib/copilot/serverAnnotations'
import { normalizeAnnotationMarkers } from '@sida/biz-ui/klineEvents'

const TARGET: AnnotationTarget = {
  owner: 'user-A',
  instrument: 'stock',
  symbol: '600111',
  market: 'CN',
  period: '1d',
}

function row(overrides: Record<string, unknown> = {}) {
  return {
    id: 91,
    symbol: '600111',
    market: 'CN',
    period: '1d',
    user_id: 'user-A',
    action_id: 7,
    price: null,
    ts: '2026-10-20',
    label: '复盘点',
    note: '那天放量突破',
    color: 'up',
    source: 'ai',
    created_at: '2026-10-20 15:00:00',
    ...overrides,
  }
}

const parseRows = (items: unknown[]) => {
  const parsed = parseAnnotationsResponse(items)
  if (!parsed.ok) throw new Error(parsed.message)
  return parsed.value
}

function markerRow(overrides: Record<string, unknown> = {}) {
  return parseRows([row(overrides)]).rows
}

describe('1. 纯时间点标注走 marker 通道（不再被当成"没有图层的废行"）', () => {
  it('只有日期没有价位的行 → 给出 marker，日期/标签/方向逐字来自服务端', () => {
    const verdict = verifyRowMarker(markerRow()[0], TARGET)
    expect(verdict.ok).toBe(true)
    if (!verdict.ok) return
    expect(verdict.marker).toMatchObject({
      annotationId: 91,
      actionId: 7,
      date: '2026-10-20',
      label: '复盘点',
      tone: 'up',
      symbol: '600111',
      market: 'CN',
      period: '1d',
      ownerId: 'user-A',
    })
  })

  it('它**不属于**价位线图层：verifyRow 如实说明走 marker 通道（不编价位）', () => {
    const verdict = verifyRow(markerRow()[0], TARGET)
    expect(verdict.ok).toBe(false)
    if (!verdict.ok) {
      expect(verdict.reason).toBe('price')
      expect(verdict.message).toContain('marker')
    }
  })

  it('带时分的时间点不画（无法证明落在哪一根 K 线）', () => {
    const verdict = verifyRowMarker(markerRow({ ts: '2026-10-20 10:00' })[0], TARGET)
    expect(verdict.ok).toBe(false)
    if (!verdict.ok) expect(verdict.message).toContain('日粒度')
  })

  it('8 位无横杠日期归一化成日粒度', () => {
    const verdict = verifyRowMarker(markerRow({ ts: '20261020' })[0], TARGET)
    expect(verdict.ok).toBe(true)
    if (verdict.ok) expect(verdict.marker.date).toBe('2026-10-20')
  })
})

describe('2. 身份/目标门禁与价位线同一套（不能只有线核对属主）', () => {
  it('他人的行 / 全局行 / 无确认动作 / 非 AI 来源 → 一律不画', () => {
    for (const overrides of [
      { user_id: 'user-B' },
      { user_id: null },
      { action_id: null },
      { source: 'user' },
    ]) {
      const verdict = verifyRowMarker(markerRow(overrides)[0], TARGET)
      expect(verdict.ok, JSON.stringify(overrides)).toBe(false)
    }
  })

  it('跨标的 / 跨周期 / 市场不可证 → 不画', () => {
    expect(verifyRowMarker(markerRow({ symbol: '600519' })[0], TARGET).ok).toBe(false)
    expect(verifyRowMarker(markerRow({ period: '5m' })[0], TARGET).ok).toBe(false)
    // 非 A 股族市场（HK）与 CN 不可互证
    expect(verifyRowMarker(markerRow({ market: 'HK' })[0], TARGET).ok).toBe(false)
  })

  it('000xxx 同号代码在个股页也拒画（标注行证明不了标的类型）', () => {
    const rows = markerRow({ symbol: '000001' })
    expect(verifyRowMarker(rows[0], { ...TARGET, symbol: '000001', market: 'SZ' }).ok).toBe(false)
    // 指数图一律拒画（入口层已拦，这里再拦一次）
    const idx = verifyRowMarker(rows[0], {
      ...TARGET,
      symbol: '000001',
      market: 'SH',
      instrument: 'index',
    })
    expect(idx.ok).toBe(false)
  })
})

describe('3. 两条通道并列（同一次读取里线是线、标记是标记）', () => {
  const rows = parseRows([
    row({ id: 91, ts: '2026-10-20', label: '复盘点' }),
    row({ id: 92, price: 32.8, ts: null, label: '支撑位', color: 'up' }),
    row({ id: 93, ts: '2026-10-21', label: '复盘点' }), // 同槽位重复确认的前一条
    row({ id: 94, ts: '2026-10-21', label: '复盘点' }), // 较新
  ]).rows

  it('价位行进 lines、时间点行进 markers，互不冒充', () => {
    const verdict = marksForTarget(rows, TARGET)
    expect(verdict.lines.map((l) => [l.annotationId, l.price])).toEqual([[92, 32.8]])
    expect(verdict.markers.map((m) => [m.annotationId, m.date])).toEqual([
      [91, '2026-10-20'],
      [94, '2026-10-21'],
    ])
    // 同 (date,label,tone) 的重复确认合并一次，留较新的 id（93 被 94 顶掉）
    expect(verdict.mergedMarkers).toBe(1)
    expect(verdict.rejected).toEqual([])
  })

  it('有价位的行不会被当成时间点标注（价格优先，不双画）', () => {
    const both = parseRows([row({ id: 95, price: 12.5, ts: '2026-10-22', label: '支撑位' })]).rows
    const verdict = marksForTarget(both, TARGET)
    expect(verdict.lines).toHaveLength(1)
    expect(verdict.markers).toHaveLength(0)
  })
})

describe('4. 确认响应（apply）→ 精确对上那一条', () => {
  const body = (overrides: Record<string, unknown> = {}, detail: Record<string, unknown> = {}) => ({
    id: 7,
    kind: 'annotation',
    status: 'applied',
    annotation_id: 91,
    annotation: {
      id: 91,
      symbol: '600111',
      market: 'CN',
      period: '1d',
      price: null,
      ts: '2026-10-20',
      label: '复盘点',
      color: 'up',
      source: 'ai',
      ...detail,
    },
    ...overrides,
  })

  it('price 为 null + 日粒度 ts → 解析通过，date 归一化', () => {
    const parsed = parseApplyResponse(body())
    expect(parsed.ok).toBe(true)
    if (parsed.ok) {
      expect(parsed.value.price).toBeNull()
      expect(parsed.value.date).toBe('2026-10-20')
    }
  })

  it('price 为 null + 带时分/缺时间点 → 拒绝（脏响应不猜日期）', () => {
    expect(parseApplyResponse(body({}, { ts: '2026-10-20 10:00' })).ok).toBe(false)
    expect(parseApplyResponse(body({}, { ts: null })).ok).toBe(false)
  })

  it('读回同一条 → 通过，且 shape=marker（调用方必须按通道分支）', () => {
    const parsed = parseApplyResponse(body())
    if (!parsed.ok) throw new Error(parsed.message)
    const rows = markerRow()
    const verdict = verifyAppliedRow(parsed.value, rows, TARGET)
    expect(verdict.ok).toBe(true)
    if (verdict.ok) {
      expect(verdict.value.shape).toBe('marker')
      if (verdict.value.shape === 'marker') {
        expect(verdict.value.marker.annotationId).toBe(91)
        expect(verdict.value.marker.date).toBe('2026-10-20')
      }
    }
  })

  it('读回日期/动作/标签与确认响应不一致 → 拒（不把别的时间点冒充刚确认的那一条）', () => {
    const parsed = parseApplyResponse(body())
    if (!parsed.ok) throw new Error(parsed.message)
    expect(verifyAppliedRow(parsed.value, markerRow({ ts: '2026-10-21' }), TARGET).ok).toBe(false)
    expect(verifyAppliedRow(parsed.value, markerRow({ action_id: 8 }), TARGET).ok).toBe(false)
    expect(verifyAppliedRow(parsed.value, markerRow({ label: '别的标注' }), TARGET).ok).toBe(false)
    // 读回里没有这个 id → 拒
    expect(verifyAppliedRow(parsed.value, markerRow({ id: 92 }), TARGET).ok).toBe(false)
  })
})

describe('5. 图表侧清洗（图表无关层）', () => {
  it('缺 id/动作 id、空标签、非日粒度 → 丢弃，不补', () => {
    const out = normalizeAnnotationMarkers([
      { annotationId: 1, actionId: 2, date: '2026-10-20', label: ' ', tone: 'up' },
      { annotationId: 0, actionId: 2, date: '2026-10-20', label: 'x', tone: 'up' },
      { annotationId: 3, actionId: 0, date: '2026-10-20', label: 'x', tone: 'up' },
      { annotationId: 4, actionId: 2, date: '2026-02-30', label: 'x', tone: 'up' },
      { annotationId: 5, actionId: 2, date: '2026-10-20 09:30', label: 'x', tone: 'up' },
    ] as never)
    expect(out).toEqual([])
  })

  it('同一 id 只留一个记号，按日期升序', () => {
    const out = normalizeAnnotationMarkers([
      { annotationId: 9, actionId: 3, date: '2026-10-21', label: 'B', tone: 'down' },
      { annotationId: 8, actionId: 3, date: '2026-10-20', label: 'A', tone: 'up' },
      { annotationId: 9, actionId: 3, date: '2026-10-21', label: 'B', tone: 'down' },
    ])
    expect(out.map((m) => m.annotationId)).toEqual([8, 9])
  })
})
