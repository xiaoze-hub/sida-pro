import { describe, it, expect } from 'vitest'
import { annotationsUrl, targetRefusal, parseAnnotationsResponse, verifyRow } from '@/lib/copilot/serverAnnotations'

const target = { owner: 'u1', instrument: 'stock' as const, symbol: '000858', market: 'CN', period: '1d' }
describe('明确标的类型的安全契约', () => {
  it('000xxx 股票可请求且请求携带类型', () => {
    expect(targetRefusal(target)).toBeNull()
    expect(annotationsUrl(target)).toContain('instrument=stock')
  })
  it('只接受服务端明确同类型记录，不接受指数或缺类型', () => {
    const row = { id: 1, symbol: '000858', market: 'SZ', period: '1d', instrument: 'stock', user_id: 'u1', action_id: 8, price: 32, label: '支撑位', source: 'ai', color: 'up' }
    const rows = parseAnnotationsResponse([row, { ...row, id: 2, instrument: 'index' }, { ...row, id: 3, instrument: null }])
    expect(rows.ok).toBe(true)
    if (!rows.ok) return
    expect(verifyRow(rows.value.rows[0], target).ok).toBe(true)
    expect(verifyRow(rows.value.rows[1], target).ok).toBe(false)
    expect(verifyRow(rows.value.rows[2], target).ok).toBe(false)
  })
})
