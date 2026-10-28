// @vitest-environment jsdom
/**
 * P2 图表价位来源与确认 —— 二次收口（2026-10-28）。
 *
 * 上一版只校验一个**调用方构造的对象**（`source/actionId/status/symbol`）。问题是：
 * 客户端 JS 与控制台都能原地 spawn 出这个对象，它不绑定价格、不对应服务端
 * CopilotAction 记录；后端虽有 `GET /copilot/annotations`，前端却从未调用，也没有
 * 服务端校验会核对这份客户端声明。把它叫"可信凭证"等于自证清白。
 *
 * 本文件钉住收口后的事实：
 *  1. 服务端标注通道未接通时，**任何** provenance（含完全伪造的"合法"对象）都拒画；
 *  2. sessionStorage 里伪造的 source/actionId 行不会被还原上图；
 *  3. 图元级旁路 `injectDecisionLevels` 同样拒画；
 *  4. 账号隔离与存储键作用域仍在；
 *  5. 用户提示准确：确认服务端提议后**暂也不会**自动上图，不误导。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { AnswerCard } from '@/components/copilot/AnswerCard'
import { parseAnswer } from '@/lib/copilot/answer-blocks'
import {
  ChartOverlayService,
  SERVER_ANNOTATION_LINK_READY,
  type OverlayProvenance,
} from '@/lib/copilot/chartOverlayService'

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

const keyFor = (userId: string, symbol = '600111') =>
  `sida_kline_overlay:v2:${encodeURIComponent(userId)}:${encodeURIComponent(symbol)}`

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
  window.addEventListener('sida-kline-overlay', onOverlay)
})

afterEach(() => {
  window.removeEventListener('sida-kline-overlay', onOverlay)
  cleanup()
  localStorage.clear()
  sessionStorage.clear()
  vi.restoreAllMocks()
})

describe('服务端标注通道未接通时，任何本地凭证都不能上图', () => {
  it('开关是关闭的（真实读取接线前不得打开）', () => {
    expect(SERVER_ANNOTATION_LINK_READY).toBe(false)
  })

  it('没有 provenance 时拒画，且不落存储、不广播', () => {
    const result = ChartOverlayService.saveOverlay('600111', LEVELS)

    expect(result.saved).toBe(false)
    expect(result.reason).toBe('server_link_unavailable')
    expect(result.message).toContain('未画线')
    expect(result.message).toContain('尚未接通')
    expect(overlayEvents).toHaveLength(0)
    expect(sessionStorage.getItem(keyFor('test-user'))).toBeNull()
    expect(ChartOverlayService.getStoredOverlay('600111')).toBeNull()
  })

  it('伪造"合法"凭证（source + actionId + applied + 同标的）同样拒画', () => {
    const result = ChartOverlayService.saveOverlay('600111', LEVELS, forged())

    expect(result.saved).toBe(false)
    expect(result.reason).toBe('server_link_unavailable')
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
      expect(result.reason, JSON.stringify(variant)).toBe('server_link_unavailable')
      expect(sessionStorage.getItem(keyFor('test-user'))).toBeNull()
    }
    expect(overlayEvents).toHaveLength(0)
  })

  it('sessionStorage 里伪造的"已确认"行不会被还原，哪怕标的一致', () => {
    sessionStorage.setItem(
      keyFor('test-user'),
      JSON.stringify([
        { price: 32.8, kind: 'support', label: '支撑 32.8', source: 'server_annotation', actionId: 999 },
        { price: 36.5, kind: 'pressure', label: '阻力 36.5', source: 'server_annotation', actionId: 999 },
      ]),
    )

    expect(ChartOverlayService.getStoredOverlay('600111')).toBeNull()
  })

  it('存储键仍按账号隔离：不跨账号清除、登出时不动任何数据', () => {
    // 伪造数据（模拟已被写入或他人写入）按账号分键存放
    sessionStorage.setItem(keyFor('user-A'), JSON.stringify([{ price: 1, kind: 'support', label: 'x' }]))

    // 切到 user-B：既读不到，也不越权删除 user-A 的键
    localStorage.setItem('token', token('user-B'))
    expect(ChartOverlayService.getStoredOverlay('600111')).toBeNull()
    ChartOverlayService.clearOverlay('600111')
    expect(sessionStorage.getItem(keyFor('user-A'))).not.toBeNull()

    // 登出后 clearOverlay 不触碰任何账号键
    localStorage.removeItem('token')
    ChartOverlayService.clearOverlay('600111')
    expect(sessionStorage.getItem(keyFor('user-A'))).not.toBeNull()

    // user-A 自己清理时只删自己的键
    localStorage.setItem('token', token('user-A'))
    ChartOverlayService.clearOverlay('600111')
    expect(sessionStorage.getItem(keyFor('user-A'))).toBeNull()
  })

  it('未登录时不写任何存储（身份是未来服务端读取作用域的前提）', () => {
    localStorage.removeItem('token')

    const result = ChartOverlayService.saveOverlay('600111', LEVELS, forged())

    expect(result.saved).toBe(false)
    expect(result.reason).toBe('no_identity')
    expect(sessionStorage.length).toBe(0)
  })
})

describe('AnswerCard 只展示文本价位，不冒充已核验标注', () => {
  it('保留支撑/阻力文本展示，但没有画线入口，并如实说明确认后暂也不会自动上图', () => {
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

    // 提示必须准确：不核验、不画线，且**现在确认也不会自动上图**（不误导）
    const note = screen.getByTestId('copilot-level-provenance-note')
    expect(note.textContent).toContain('文本识别')
    expect(note.textContent).toContain('未经服务端核验')
    expect(note.textContent).toContain('未画到 K 线')
    expect(note.textContent).toContain('尚未接通')
    expect(note.textContent).toContain('暂也不会自动上图')
    expect(note.textContent).not.toContain('后再标注')

    // 点价位 chip 仍只是"看/传锚点"，不触发票据外画线
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

describe('图元级旁路 injectDecisionLevels', () => {
  it('通道未接通时拒画，即使调用方递上伪造凭证', () => {
    const mockSeries = {
      createPriceLine: vi.fn((config) => config),
      removePriceLine: vi.fn(),
    }

    const drawn = ChartOverlayService.injectDecisionLevels(mockSeries, LEVELS, forged())

    expect(drawn).toBe(false)
    expect(mockSeries.createPriceLine).not.toHaveBeenCalled()
  })
})
