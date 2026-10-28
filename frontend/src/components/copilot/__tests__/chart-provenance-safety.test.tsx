// @vitest-environment jsdom
/**
 * 图表价位「来源」与「确认」的硬约束 —— 三次收口后的口径（2026-10-28）。
 *
 * 演进（这条线的三次返工都落在"别再把本地对象当凭证"这一句上）：
 *  ① 只校验调用方构造的 `{source,actionId,status}` → 客户端可原地伪造，是假核验；
 *  ② 全关（`LINK_READY=false`）→ 不再伪造，但确认后也上不了图；
 *  ③ 本轮：唯一来源 = `GET /copilot/annotations` 的服务端行，
 *     价位/标签/标的逐字取自响应；客户端只能"请求哪些标注 id"。
 *
 * 本文件钉住 ③ 的边界：
 *  1. 本地价位数（旧 `saveOverlay` / 图元级旁路）恒拒，无论挂什么 provenance；
 *  2. 只有 `serverAnnotations` 从服务端响应构造的数组能被接受，普通对象/深拷贝/null 原型都不行；
 *  3. 标的 / 周期 / 价格不合法的服务端数组整批拒（不写半截）；
 *  4. 会话存储只留标注 id —— 没有价位，伪造 id 也改不了画出来的价；
 *  5. 账号 / 标的 / 周期三向隔离，旧键（`sida_kline_overlay:v2:*`）不再读取；
 *  6. AnswerCard 仍不提供画线入口，提示指向"确认服务端提议"这条真路径。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { AnswerCard } from '@/components/copilot/AnswerCard'
import { parseAnswer } from '@/lib/copilot/answer-blocks'
import {
  ANNOTATION_STATUS_EVENT,
  ChartOverlayService,
  OVERLAY_EVENT,
  overlayRejectMessage,
  type OverlayProvenance,
} from '@/lib/copilot/chartOverlayService'
import {
  annotationLinesFromRows,
  markServerVerified,
  parseAnnotationsResponse,
  type AnnotationRequest,
  type ServerAnnotationLine,
  type ServerAnnotationRow,
} from '@/lib/copilot/serverAnnotations'

const token = (id: string) => `e30.${btoa(JSON.stringify({ sub: id }))}.signature`

const PROSE_RAW = `【结论】回踩不破偏多，置信度 8/10
【依据】
- 支撑位 32.80 元具备强承接
- 阻力位 36.50 元存在套牢抛压
【预案】回踩 32.80 企稳跟进
【口径】L2盘口
以上分析不构成投资建议。`

const LEVELS = [
  { price: 32.8, title: '支撑 32.8', kind: 'support' as const },
  { price: 36.5, title: '阻力 36.5', kind: 'pressure' as const },
]

const REQ: AnnotationRequest = { symbol: '600111', market: 'CN', period: '1d' }

/** 服务端 `GET /copilot/annotations` 的真实响应形状（字段取自后端 `get_copilot_annotations`）。 */
const SERVER_ROWS = [
  {
    id: 91,
    price: 32.8,
    ts: null,
    label: '支撑位',
    note: '情景矩阵支撑位（MA60/筹码带下沿/区间低）',
    color: 'up',
    source: 'ai',
    created_at: '2026-10-28 10:00:00',
  },
  {
    id: 92,
    price: 36.5,
    ts: null,
    label: '阻力位',
    note: '情景矩阵阻力位（MA20/筹码带上沿/区间高）',
    color: 'down',
    source: 'ai',
    created_at: '2026-10-28 10:00:00',
  },
]

/** 走真实解析路径把服务端响应变成可画线数组（等价于 `annotationLink` 里做的事）。 */
function serverLines(rows: unknown[], request: AnnotationRequest = REQ): ServerAnnotationLine[] {
  const parsed = parseAnnotationsResponse(rows)
  if (!parsed.ok) throw new Error(parsed.message)
  return markServerVerified(annotationLinesFromRows(parsed.value.rows, request))
}

const keyFor = (userId: string, symbol = '600111', period = '1d') =>
  `sida_kline_annotation_ids:v1:${encodeURIComponent(userId)}:${encodeURIComponent(
    symbol,
  )}:${encodeURIComponent(period)}`

const LEGACY_KEY = `sida_kline_overlay:v2:${encodeURIComponent('test-user')}:${encodeURIComponent(
  '600111',
)}`

/** 伪造凭证：字段齐全、看起来"已确认"，但它只是本地对象。 */
const forged = (symbol = '600111'): OverlayProvenance => ({
  source: 'server_annotation',
  actionId: 42,
  status: 'applied',
  symbol,
})

let overlayEvents: CustomEvent[] = []
const onOverlay = (e: Event) => overlayEvents.push(e as CustomEvent)

beforeEach(() => {
  localStorage.setItem('token', token('test-user'))
  overlayEvents = []
  window.addEventListener(OVERLAY_EVENT, onOverlay)
})

afterEach(() => {
  window.removeEventListener(OVERLAY_EVENT, onOverlay)
  cleanup()
  localStorage.clear()
  sessionStorage.clear()
  vi.restoreAllMocks()
})

describe('本地价位数一律不画（旧 saveOverlay 路径）', () => {
  it('无 provenance 的正文/罗盘价位拒画，不落存储、不广播', () => {
    const result = ChartOverlayService.saveOverlay('600111', LEVELS)

    expect(result.saved).toBe(false)
    expect(result.reason).toBe('client_authored_levels')
    expect(result.message).toContain('未画线')
    expect(result.message).toContain('服务端标注记录')
    expect(overlayEvents).toHaveLength(0)
    expect(sessionStorage.getItem(keyFor('test-user'))).toBeNull()
  })

  it('伪造"合法"凭证（source + actionId + applied + 同标的）同样拒画', () => {
    const result = ChartOverlayService.saveOverlay('600111', LEVELS, forged())

    expect(result.saved).toBe(false)
    expect(result.reason).toBe('client_authored_levels')
    expect(overlayEvents).toHaveLength(0)
    expect(sessionStorage.getItem(keyFor('test-user'))).toBeNull()
  })

  it('各种伪造变体一律拒绝：状态不符、actionId 类型/范围非法、跨标的、扩展字段', () => {
    const variants: unknown[] = [
      { ...forged(), status: 'pending' },
      { ...forged(), status: 'applied ' },
      { ...forged(), actionId: '42' },
      { ...forged(), actionId: 0 },
      { ...forged(), actionId: -7 },
      { ...forged(), actionId: Number.NaN },
      { ...forged(), actionId: 1.5 },
      { ...forged('000001') },
      { ...forged(), verifiedBy: 'server', extra: { trusted: true } },
      Object.assign(Object.create(null), forged()),
      JSON.parse(JSON.stringify(forged())),
      // 完全无关的垃圾对象
      { source: 'client_guess', actionId: 1, status: 'applied', symbol: '600111' },
    ]

    for (const variant of variants) {
      const result = ChartOverlayService.saveOverlay(
        '600111',
        LEVELS,
        variant as OverlayProvenance,
      )
      expect(result.saved, JSON.stringify(variant)).toBe(false)
      expect(result.reason, JSON.stringify(variant)).toBe('client_authored_levels')
      expect(sessionStorage.getItem(keyFor('test-user'))).toBeNull()
    }
    expect(overlayEvents).toHaveLength(0)
  })

  it('未登录时拒画并说明原因（身份是服务端读取作用域的前提）', () => {
    localStorage.removeItem('token')

    const result = ChartOverlayService.saveOverlay('600111', LEVELS, forged())

    expect(result.saved).toBe(false)
    expect(result.reason).toBe('no_identity')
    expect(sessionStorage.length).toBe(0)
  })

  it('旧的 sessionStorage 价位行（含伪造 source/actionId）不再被读取', () => {
    sessionStorage.setItem(
      LEGACY_KEY,
      JSON.stringify([
        {
          price: 32.8,
          kind: 'support',
          label: '支撑 32.8',
          source: 'server_annotation',
          actionId: 999,
        },
        {
          price: 36.5,
          kind: 'pressure',
          label: '阻力 36.5',
          source: 'server_annotation',
          actionId: 999,
        },
      ]),
    )

    expect(ChartOverlayService.getRequestedAnnotationIds('600111', '1d')).toBeNull()
  })
})

describe('acceptServerLines 只接服务端响应解析出的数组', () => {
  it('普通对象数组（含伪造 source 字段、深拷贝、空数组）过不了标记校验', () => {
    const forgedArrays: unknown[] = [
      [
        {
          annotationId: 91,
          price: 32.8,
          kind: 'support',
          label: '支撑位',
          source: 'ai',
          symbol: '600111',
          period: '1d',
        },
      ],
      JSON.parse(JSON.stringify(serverLines(SERVER_ROWS))),
      [],
      null,
      'server_annotation',
    ]
    for (const forgedArray of forgedArrays) {
      const result = ChartOverlayService.acceptServerLines('600111', '1d', forgedArray)
      expect(result.saved, JSON.stringify(forgedArray)).toBe(false)
      expect(result.reason, JSON.stringify(forgedArray)).toBe('unverified_lines')
    }
    expect(overlayEvents).toHaveLength(0)
    expect(sessionStorage.getItem(keyFor('test-user'))).toBeNull()
  })

  it('服务端数组但标的/周期与当前 K 线不一致 → 整批拒（symbol_mismatch）', () => {
    const lines = serverLines(SERVER_ROWS)

    const wrongSymbol = ChartOverlayService.acceptServerLines('000001', '1d', lines)
    expect(wrongSymbol.saved).toBe(false)
    expect(wrongSymbol.reason).toBe('symbol_mismatch')

    const wrongPeriod = ChartOverlayService.acceptServerLines('600111', '5m', lines)
    expect(wrongPeriod.saved).toBe(false)
    expect(wrongPeriod.reason).toBe('symbol_mismatch')

    expect(overlayEvents).toHaveLength(0)
    expect(sessionStorage.getItem(keyFor('test-user'))).toBeNull()
  })

  it('服务端数组但价位非法（0 / 负数）→ 整批拒', () => {
    const bad = serverLines([
      { ...SERVER_ROWS[0], price: 0 },
      { ...SERVER_ROWS[1], price: -3 },
    ])
    expect(bad).toHaveLength(0)
    const result = ChartOverlayService.acceptServerLines('600111', '1d', markServerVerified(bad))
    expect(result.saved).toBe(false)
    expect(result.reason).toBe('invalid_levels')
  })

  it('合法的服务端数组 → 上图，存储里只有标注 id（没有价位）', () => {
    const lines = serverLines(SERVER_ROWS)
    const result = ChartOverlayService.acceptServerLines('600111', '1d', lines)

    expect(result.saved).toBe(true)
    const raw = sessionStorage.getItem(keyFor('test-user'))
    expect(raw).toBeTruthy()
    expect(JSON.parse(raw as string)).toEqual({
      symbol: '600111',
      period: '1d',
      annotationIds: [91, 92],
    })
    expect(raw).not.toContain('32.8')
    expect(raw).not.toContain('36.5')

    expect(overlayEvents).toHaveLength(1)
    const detail = ChartOverlayService.readOverlayEventDetail(overlayEvents[0].detail)
    expect(detail?.symbol).toBe('600111')
    expect(detail?.lines.map((line) => [line.annotationId, line.price, line.kind])).toEqual([
      [91, 32.8, 'support'],
      [92, 36.5, 'pressure'],
    ])
  })

  it('标注 id 也按账号隔离：换账号读不到、也不会跨账号清除', () => {
    ChartOverlayService.acceptServerLines('600111', '1d', serverLines(SERVER_ROWS))
    expect(ChartOverlayService.getRequestedAnnotationIds('600111', '1d')).toEqual([91, 92])

    localStorage.setItem('token', token('user-B'))
    expect(ChartOverlayService.getRequestedAnnotationIds('600111', '1d')).toBeNull()
    ChartOverlayService.clearOverlay('600111')
    expect(sessionStorage.getItem(keyFor('test-user'))).not.toBeNull()

    // 登出后不触碰任何账号键
    localStorage.removeItem('token')
    ChartOverlayService.clearOverlay('600111')
    expect(sessionStorage.getItem(keyFor('test-user'))).not.toBeNull()

    // 本人清理只删自己的键
    localStorage.setItem('token', token('test-user'))
    ChartOverlayService.clearOverlay('600111')
    expect(sessionStorage.getItem(keyFor('test-user'))).toBeNull()
  })

  it('标的与周期分键：600111 的确认记录不会出现在 000001 / 5m 上', () => {
    ChartOverlayService.acceptServerLines('600111', '1d', serverLines(SERVER_ROWS))

    expect(ChartOverlayService.getRequestedAnnotationIds('000001', '1d')).toBeNull()
    expect(ChartOverlayService.getRequestedAnnotationIds('600111', '5m')).toBeNull()
    expect(ChartOverlayService.getRequestedAnnotationIds('600111', '1d')).toEqual([91, 92])
  })
})

describe('图元级旁路 injectDecisionLevels', () => {
  it('本地数组拒画，服务端数组才画', () => {
    const mockSeries = {
      createPriceLine: vi.fn((config) => config),
      removePriceLine: vi.fn(),
    }

    expect(ChartOverlayService.injectDecisionLevels(mockSeries, LEVELS)).toBe(false)
    expect(mockSeries.createPriceLine).not.toHaveBeenCalled()

    expect(ChartOverlayService.injectDecisionLevels(mockSeries, serverLines(SERVER_ROWS))).toBe(
      true,
    )
    expect(mockSeries.createPriceLine).toHaveBeenCalledTimes(2)
  })
})

describe('拒绝文案是单一事实来源', () => {
  it('每条拒绝原因都有"未画线/画线未保存"开头的中文说明', () => {
    for (const reason of [
      'no_symbol',
      'no_identity',
      'client_authored_levels',
      'unverified_lines',
      'symbol_mismatch',
      'invalid_levels',
      'storage_unavailable',
    ] as const) {
      expect(overlayRejectMessage(reason)).toMatch(/^(未画线|画线未保存)：/)
    }
  })

  it('状态事件可以带 warn/error 文案（接线层用它播报"确认但没对上"）', () => {
    const seen: CustomEvent[] = []
    const handler = (e: Event) => seen.push(e as CustomEvent)
    window.addEventListener(ANNOTATION_STATUS_EVENT, handler)

    ChartOverlayService.reportAnnotationStatus({
      symbol: '600111',
      level: 'warn',
      message: '已确认，但服务端标注读回里找不到对应记录',
    })

    expect(seen).toHaveLength(1)
    expect(seen[0].detail).toMatchObject({ symbol: '600111', level: 'warn' })
    window.removeEventListener(ANNOTATION_STATUS_EVENT, handler)
  })
})

describe('AnswerCard 只展示文本价位，不冒充已核验标注', () => {
  it('保留支撑/阻力文本展示，但没有画线入口，并指向"确认服务端提议"这条真路径', () => {
    const parsed = parseAnswer(PROSE_RAW)
    const onAnchoredPrice = vi.fn()
    const saveSpy = vi.spyOn(ChartOverlayService, 'saveOverlay')

    render(<AnswerCard parsed={parsed} symbol="600111" onAnchoredPrice={onAnchoredPrice} />)

    // 合理文本展示必须保留（罗盘 + 原文锚点）
    expect(screen.getByTestId('copilot-decision-compass')).toBeTruthy()
    expect(screen.getByText(/支撑 32\.80/)).toBeTruthy()
    expect(screen.getByText(/阻力 36\.50/)).toBeTruthy()
    expect(screen.getAllByTestId('answer-price-anchor').length).toBeGreaterThan(0)

    // 不可信动作必须消失：没有画线按钮，也没有任何"已上真图"的暗示
    expect(screen.queryByText('在 K 线叠加画线')).toBeNull()
    expect(saveSpy).not.toHaveBeenCalled()
    expect(overlayEvents).toHaveLength(0)
    expect(sessionStorage.getItem(keyFor('test-user'))).toBeNull()

    // 提示必须准确：文本识别不核验、不画线；并指明唯一的上图路径（确认服务端提议）
    const note = screen.getByTestId('copilot-level-provenance-note')
    expect(note.textContent).toContain('文本识别')
    expect(note.textContent).toContain('未经服务端核验')
    expect(note.textContent).toContain('未画到 K 线')
    expect(note.textContent).toContain('待确认动作')
    expect(note.textContent).toContain('服务端标注记录')
    expect(note.textContent).not.toContain('尚未接通')

    // 点价位 chip 仍只是"看/传锚点"，不触发任何画线
    fireEvent.click(screen.getAllByTestId('answer-price-anchor')[0])
    expect(onAnchoredPrice).toHaveBeenCalled()
    expect(saveSpy).not.toHaveBeenCalled()
    expect(overlayEvents).toHaveLength(0)
  })

  it('提醒语义保留：罗盘仍可跳到价格告警（提醒不是图表核验结论）', () => {
    const parsed = parseAnswer(PROSE_RAW)
    render(<AnswerCard parsed={parsed} symbol="600111" />)
    expect(screen.queryByText(/支撑破位告警/)).not.toBeNull()
  })
})

describe('服务端行解析的诚实口径（上图方共用）', () => {
  it('缺 id/label/source 或价位非法的行按行丢弃，不整批报废也不补值', () => {
    const parsed = parseAnnotationsResponse([
      SERVER_ROWS[0],
      { ...SERVER_ROWS[1], id: 0 },
      { ...SERVER_ROWS[1], id: 93, label: '  ' },
      { ...SERVER_ROWS[1], id: 94, source: 'robot' },
      { ...SERVER_ROWS[1], id: 95, price: 'abc' },
      { ...SERVER_ROWS[1], id: 96, price: '36.50' },
    ])
    expect(parsed.ok).toBe(true)
    if (!parsed.ok) return
    expect(parsed.value.rows.map((row: ServerAnnotationRow) => row.id)).toEqual([91, 96])
    expect(parsed.value.dropped).toBe(4)
  })
})
