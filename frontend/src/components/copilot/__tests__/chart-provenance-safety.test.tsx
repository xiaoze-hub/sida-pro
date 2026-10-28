// @vitest-environment jsdom
/**
 * P2 图表价位来源与确认（2026-10-28）。
 *
 * 背景：服务端 `propose_scenario_annotations` 只写 **待确认** 的 CopilotAction，
 * 用户点确认后 `apply_action` 才落 KlineAnnotation，图表侧本应只读 `/copilot/annotations`。
 * 现实是前端从未调用该接口，`AnswerCard` 曾用**正则**从回答正文提价，
 * 直接 `saveOverlay` 进 sessionStorage 并在个股工作台画成真 K 线 —— 等于把散文数字
 * 冒充「已核验标注」，且用户没有任何确认动作。
 *
 * 本文件钉住止血后的事实：
 *  1. 没有服务端来源凭证（actionId + applied + 标的一致）→ 一律拒画，并说明原因；
 *  2. AnswerCard 只展示文本价位，不再提供「一键画真图」的假动作；
 *  3. 历史遗留的（无 provenance）会话画线不被还原；
 *  4. 真实凭证存在时仍可保存，且跨账号 / 跨标的隔离。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { AnswerCard } from '@/components/copilot/AnswerCard'
import { parseAnswer } from '@/lib/copilot/answer-blocks'
import {
  ChartOverlayService,
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

const KEY = 'sida_kline_overlay:v2:test-user:600111'

/** 真实凭证：服务端 apply 成功返回的动作。 */
const verified = (symbol = '600111'): OverlayProvenance => ({
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

describe('画线必须带服务端来源凭证', () => {
  it('没有凭证时拒绝写入、拒绝广播（散文正则价位不得变成真 K 线）', () => {
    const result = ChartOverlayService.saveOverlay('600111', [
      { price: 32.8, title: '支撑 32.8', kind: 'support' },
    ])

    expect(result.saved).toBe(false)
    expect(result.reason).toBe('unverified_source')
    // 用户可见文案必须直说"未画线"和原因，不能静默失败
    expect(result.message).toContain('未画线')
    expect(result.message).toContain('未经服务端核验')
    expect(sessionStorage.getItem(KEY)).toBeNull()
    expect(overlayEvents).toHaveLength(0)
    expect(ChartOverlayService.getStoredOverlay('600111')).toBeNull()
  })

  it('只有 pending 提议（用户没确认）时同样拒绝', () => {
    const result = ChartOverlayService.saveOverlay(
      '600111',
      [{ price: 32.8, title: '支撑 32.8', kind: 'support' }],
      { ...verified(), status: 'pending' } as unknown as OverlayProvenance,
    )

    expect(result.saved).toBe(false)
    expect(result.reason).toBe('unverified_source')
    expect(sessionStorage.getItem(KEY)).toBeNull()
  })

  it('凭证标的与画线标的不一致时拒绝（跨股票隔离）', () => {
    const result = ChartOverlayService.saveOverlay(
      '600111',
      [{ price: 32.8, title: '支撑 32.8', kind: 'support' }],
      verified('000001'),
    )

    expect(result.saved).toBe(false)
    expect(result.reason).toBe('symbol_mismatch')
    expect(sessionStorage.getItem(KEY)).toBeNull()
    expect(overlayEvents).toHaveLength(0)
  })

  it('凭证合法但价位非法（NaN / 非正 / 未知 kind）时整体拒绝，不写半截数据', () => {
    const result = ChartOverlayService.saveOverlay(
      '600111',
      [
        { price: 32.8, title: '支撑 32.8', kind: 'support' },
        { price: Number.NaN, title: '阻力 NaN', kind: 'pressure' },
      ],
      verified(),
    )

    expect(result.saved).toBe(false)
    expect(result.reason).toBe('invalid_levels')
    expect(sessionStorage.getItem(KEY)).toBeNull()
  })

  it('真实凭证下可保存并广播，且画线带上来源标记', () => {
    const result = ChartOverlayService.saveOverlay(
      '600111',
      [
        { price: 32.8, title: '支撑 32.8', kind: 'support' },
        { price: 36.5, title: '阻力 36.5', kind: 'pressure' },
      ],
      verified(),
    )

    expect(result.saved).toBe(true)
    expect(overlayEvents).toHaveLength(1)
    const detail = overlayEvents[0].detail as { symbol: string; lines: unknown[] }
    expect(detail.symbol).toBe('600111')
    expect(detail.lines).toHaveLength(2)

    const stored = ChartOverlayService.getStoredOverlay('600111')
    expect(stored).toHaveLength(2)
    expect(stored![0]).toMatchObject({
      price: 32.8,
      kind: 'support',
      label: '支撑 32.8',
      source: 'server_annotation',
      actionId: 42,
    })
  })

  it('历史遗留（无凭证）的画线不会被还原，避免旧的不安全数据继续上真图', () => {
    sessionStorage.setItem(
      KEY,
      JSON.stringify([{ price: 32.8, kind: 'support', label: '支撑 32.8' }]),
    )

    expect(ChartOverlayService.getStoredOverlay('600111')).toBeNull()
  })

  it('凭证画线仍按账号隔离：换账号读不到，切回可恢复', () => {
    ChartOverlayService.saveOverlay(
      '600111',
      [{ price: 32.8, title: '支撑 32.8', kind: 'support' }],
      verified(),
    )
    localStorage.setItem('token', token('other-user'))
    expect(ChartOverlayService.getStoredOverlay('600111')).toBeNull()
    localStorage.setItem('token', token('test-user'))
    expect(ChartOverlayService.getStoredOverlay('600111')).toHaveLength(1)
  })
})

describe('AnswerCard 只展示文本价位，不冒充已核验标注', () => {
  it('保留支撑/阻力文本展示，但不再提供"一键画真图"，并明确提示来源未确认', () => {
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
    expect(sessionStorage.getItem(KEY)).toBeNull()

    // 明确告知来源未确认
    const note = screen.getByTestId('copilot-level-provenance-note')
    expect(note.textContent).toContain('文本识别')
    expect(note.textContent).toContain('未经服务端核验')
    expect(note.textContent).toContain('未画到 K 线')

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
