// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { ChartOverlayService } from '@/lib/copilot/chartOverlayService'
import { AnswerCard } from '@/components/copilot/AnswerCard'
import { parseAnswer } from '@/lib/copilot/answer-blocks'

const token = (id: string) => `e30.${btoa(JSON.stringify({sub: id}))}.signature`
const lines = [{price: 32.8, title: '支撑 32.8', kind: 'support' as const}]
/**
 * 2026-10-28 P2：画线必须带服务端确认凭证（apply 成功的那条动作），否则一律拒画。
 * 这里用一份合法凭证专门验证"隔离 / 故障"维度，凭证本身的校验见 chart-provenance-safety。
 */
const proven = { source: 'server_annotation' as const, actionId: 7, status: 'applied' as const, symbol: '600111' }
afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear(); vi.restoreAllMocks() })

describe('画线账号隔离与故障处理', () => {
  it('切换账号和登出后不能读取其他账号画线，切回可恢复', () => {
    localStorage.setItem('token', token('user-A'))
    expect(ChartOverlayService.saveOverlay('600111', lines, proven).saved).toBe(true)
    localStorage.setItem('token', token('user-B'))
    expect(ChartOverlayService.getStoredOverlay('600111')).toBeNull()
    ChartOverlayService.clearOverlay('600111')
    localStorage.removeItem('token')
    expect(ChartOverlayService.getStoredOverlay('600111')).toBeNull()
    localStorage.setItem('token', token('user-A'))
    expect(ChartOverlayService.getStoredOverlay('600111')).toEqual([
      {price: 32.8, kind: 'support', label: '支撑 32.8', source: 'server_annotation', actionId: 7},
    ])
  })

  it('身份缺失时拒画；会话存储失败时如实返回失败（不伪成功、不广播）', () => {
    // 身份缺失 ⇒ 没有按账号隔离的 key，直接拒画并说明原因
    expect(ChartOverlayService.saveOverlay('600111', lines, proven)).toMatchObject({
      saved: false,
      reason: 'no_identity',
    })

    // 存储不可用 ⇒ 失败如实返回，且不广播"已画线"事件
    localStorage.setItem('token', token('user-A'))
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('unavailable') })
    const event = vi.fn()
    window.addEventListener('sida-kline-overlay', event)
    const result = ChartOverlayService.saveOverlay('600111', lines, proven)
    expect(result.saved).toBe(false)
    expect(result.reason).toBe('storage_unavailable')
    expect(result.message).toContain('未保存')
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
