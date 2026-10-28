// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { ChartOverlayService } from '@/lib/copilot/chartOverlayService'
import { AnswerCard } from '@/components/copilot/AnswerCard'
import { parseAnswer } from '@/lib/copilot/answer-blocks'

const token = (id: string) => `e30.${btoa(JSON.stringify({sub: id}))}.signature`
const lines = [{price: 32.8, title: '支撑 32.8', kind: 'support' as const}]
const keyFor = (id: string) =>
  `sida_kline_overlay:v2:${encodeURIComponent(id)}:${encodeURIComponent('600111')}`
/**
 * 2026-10-28 P2 二次收口：服务端标注通道接通前，本地对象 / sessionStorage 都不是可信来源。
 * 这里钉住"账号隔离仍在、失败必须如实"的维度（伪造 payload 的拒绝见 chart-provenance-safety）。
 */
afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear(); vi.restoreAllMocks() })

describe('画线账号隔离与故障处理', () => {
  it('画线按账号分键：换账号/登出都读不到，也不会跨账号清除', () => {
    localStorage.setItem('token', token('user-A'))
    // 无论伪造与否，写入一律被拒 ⇒ 没有任何画线数据产生
    expect(ChartOverlayService.saveOverlay('600111', lines, {
      source: 'server_annotation', actionId: 7, status: 'applied', symbol: '600111',
    }).saved).toBe(false)
    expect(sessionStorage.getItem(keyFor('user-A'))).toBeNull()

    // 手工塞入的伪造行（模拟他人/旧版本写入）同样读不出来
    sessionStorage.setItem(keyFor('user-A'), JSON.stringify([
      {price: 32.8, kind: 'support', label: '支撑 32.8', source: 'server_annotation', actionId: 7},
    ]))

    localStorage.setItem('token', token('user-B'))
    expect(ChartOverlayService.getStoredOverlay('600111')).toBeNull()
    ChartOverlayService.clearOverlay('600111')
    expect(sessionStorage.getItem(keyFor('user-A'))).not.toBeNull()

    localStorage.removeItem('token')
    expect(ChartOverlayService.getStoredOverlay('600111')).toBeNull()
    ChartOverlayService.clearOverlay('600111')
    expect(sessionStorage.getItem(keyFor('user-A'))).not.toBeNull()

    localStorage.setItem('token', token('user-A'))
    expect(ChartOverlayService.getStoredOverlay('600111')).toBeNull()
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

  it('AnswerCard 不再用正文价位画线：没有画线入口，也不会弹"画线失败"对话框', () => {
    const alert = vi.spyOn(window, 'alert').mockImplementation(() => {})
    const parsed = parseAnswer('【结论】观察\n【依据】\n- 支撑位 32.80 元\n- 阻力位 36.50 元\n【预案】观望\n【口径】行情')
    render(<AnswerCard parsed={parsed} symbol="600111" />)
    expect(screen.queryByText('在 K 线叠加画线')).toBeNull()
    expect(screen.getByTestId('copilot-level-provenance-note')).toBeTruthy()
    expect(alert).not.toHaveBeenCalled()
  })
})
