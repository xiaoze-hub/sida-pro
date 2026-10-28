// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { ChartOverlayService } from '@/lib/copilot/chartOverlayService'
import { AnswerCard } from '@/components/copilot/AnswerCard'
import { parseAnswer } from '@/lib/copilot/answer-blocks'
import {
  annotationLinesFromRows,
  markServerVerified,
  parseAnnotationsResponse,
} from '@/lib/copilot/serverAnnotations'

const token = (id: string) => `e30.${btoa(JSON.stringify({ sub: id }))}.signature`
const lines = [{ price: 32.8, title: '支撑 32.8', kind: 'support' as const }]
/** 新口径：会话存储只放"本会话确认过的服务端标注 id"（按账号+标的+周期分键）。 */
const keyFor = (id: string, symbol = '600111', period = '1d') =>
  `sida_kline_annotation_ids:v1:${encodeURIComponent(id)}:${encodeURIComponent(
    symbol,
  )}:${encodeURIComponent(period)}`

/** 服务端行 → 已标记数组（唯一合法来源）。 */
function serverLines() {
  const parsed = parseAnnotationsResponse([
    { id: 7, price: 32.8, ts: null, label: '支撑位', note: null, color: 'up', source: 'ai', created_at: '' },
  ])
  if (!parsed.ok) throw new Error(parsed.message)
  return markServerVerified(
    annotationLinesFromRows(parsed.value.rows, { symbol: '600111', market: 'CN', period: '1d' }),
  )
}

/**
 * 2026-10-28 P2 三次收口：价位只能来自服务端标注响应（`lib/copilot/serverAnnotations`），
 * 本地对象 / 旧 sessionStorage 行都不是来源。这里钉住"账号隔离仍在、失败必须如实"的维度
 * （未标记数组的拒绝见 chart-provenance-safety，端到端见 annotation-e2e-link）。
 */
afterEach(() => {
  cleanup()
  localStorage.clear()
  sessionStorage.clear()
  vi.restoreAllMocks()
})

describe('画线账号隔离与故障处理', () => {
  it('标注 id 信封按账号分键：换账号/登出都读不到，也不会跨账号清除', () => {
    localStorage.setItem('token', token('user-A'))
    // 本地价位写入一律被拒 ⇒ 不会产生任何存储
    expect(
      ChartOverlayService.saveOverlay('600111', lines, {
        source: 'server_annotation',
        actionId: 7,
        status: 'applied',
        symbol: '600111',
      }).saved,
    ).toBe(false)
    expect(sessionStorage.getItem(keyFor('user-A'))).toBeNull()

    // 服务端数组才写得进（只写 id）
    expect(ChartOverlayService.acceptServerLines('600111', '1d', serverLines()).saved).toBe(true)
    expect(sessionStorage.getItem(keyFor('user-A'))).not.toBeNull()

    localStorage.setItem('token', token('user-B'))
    expect(ChartOverlayService.getRequestedAnnotationIds('600111', '1d')).toBeNull()
    ChartOverlayService.clearOverlay('600111')
    expect(sessionStorage.getItem(keyFor('user-A'))).not.toBeNull()

    localStorage.removeItem('token')
    expect(ChartOverlayService.getRequestedAnnotationIds('600111', '1d')).toBeNull()
    ChartOverlayService.clearOverlay('600111')
    expect(sessionStorage.getItem(keyFor('user-A'))).not.toBeNull()

    localStorage.setItem('token', token('user-A'))
    expect(ChartOverlayService.getRequestedAnnotationIds('600111', '1d')).toEqual([7])
    ChartOverlayService.clearOverlay('600111')
    expect(sessionStorage.getItem(keyFor('user-A'))).toBeNull()
  })

  it('身份缺失时拒画并说明原因，不写存储、不广播', () => {
    const event = vi.fn()
    window.addEventListener('sida-kline-overlay', event)
    const result = ChartOverlayService.saveOverlay('600111', lines)
    expect(result).toMatchObject({ saved: false, reason: 'no_identity' })
    expect(result.message).toContain('未画线')
    expect(sessionStorage.length).toBe(0)
    expect(event).not.toHaveBeenCalled()
    window.removeEventListener('sida-kline-overlay', event)
  })

  it('未登录时连服务端数组也不写（服务端读取作用域的前提是身份）', () => {
    localStorage.removeItem('token')
    const result = ChartOverlayService.acceptServerLines('600111', '1d', serverLines())
    expect(result).toMatchObject({ saved: false, reason: 'no_identity' })
    expect(sessionStorage.length).toBe(0)
  })

  it('AnswerCard 不再用正文价位画线：没有画线入口，也不会弹"画线失败"对话框', () => {
    const alert = vi.spyOn(window, 'alert').mockImplementation(() => {})
    const parsed = parseAnswer(
      '【结论】观察\n【依据】\n- 支撑位 32.80 元\n- 阻力位 36.50 元\n【预案】观望\n【口径】行情',
    )
    render(<AnswerCard parsed={parsed} symbol="600111" />)
    expect(screen.queryByText('在 K 线叠加画线')).toBeNull()
    expect(screen.getByTestId('copilot-level-provenance-note')).toBeTruthy()
    expect(alert).not.toHaveBeenCalled()
  })
})
