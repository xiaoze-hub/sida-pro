// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { ChartOverlayService } from '@/lib/copilot/chartOverlayService'
import { AnswerCard } from '@/components/copilot/AnswerCard'
import { parseAnswer } from '@/lib/copilot/answer-blocks'

const token = (id: string) => `e30.${btoa(JSON.stringify({sub: id}))}.signature`
const lines = [{price: 32.8, title: '支撑 32.8', kind: 'support' as const}]
afterEach(() => { cleanup(); localStorage.clear(); sessionStorage.clear(); vi.restoreAllMocks() })

describe('画线账号隔离与故障处理', () => {
  it('切换账号和登出后不能读取其他账号画线，切回可恢复', () => {
    localStorage.setItem('token', token('user-A'))
    expect(ChartOverlayService.saveOverlay('600111', lines)).toBe(true)
    localStorage.setItem('token', token('user-B'))
    expect(ChartOverlayService.getStoredOverlay('600111')).toBeNull()
    ChartOverlayService.clearOverlay('600111')
    localStorage.removeItem('token')
    expect(ChartOverlayService.getStoredOverlay('600111')).toBeNull()
    localStorage.setItem('token', token('user-A'))
    expect(ChartOverlayService.getStoredOverlay('600111')).toEqual([{price: 32.8, kind:'support', label:'支撑 32.8'}])
  })
  it('身份缺失或会话存储失败时禁止伪成功并提示用户', () => {
    localStorage.setItem('token', token('user-A'))
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('unavailable') })
    const alert = vi.spyOn(window, 'alert').mockImplementation(() => {})
    const event = vi.fn()
    window.addEventListener('sida-kline-overlay', event)
    const parsed = parseAnswer('【结论】观察\n【依据】\n- 支撑位 32.80 元\n- 阻力位 36.50 元\n【预案】观望\n【口径】行情')
    render(<AnswerCard parsed={parsed} symbol="600111" />)
    fireEvent.click(screen.getByText('在 K 线叠加画线'))
    expect(alert).toHaveBeenCalled()
    expect(event).not.toHaveBeenCalled()
    window.removeEventListener('sida-kline-overlay', event)
  })
})
