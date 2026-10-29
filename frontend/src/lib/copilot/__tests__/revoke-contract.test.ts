/**
 * 标注撤销契约（2026-10-29, P2 撤销闭合）：解析与 URL 构造的纯单元测试。
 *
 * 后端 `DELETE /api/copilot/annotations/{id}?reason=` 的成功响应是
 * `{ok:true, status:'revoked', annotation_id, action_id, action_status:'rolled_back', ...}`。
 * 前端**不许**看到 `ok:true` 就当成"已删"：
 *  - 必须逐字核对 `status='revoked'`、`action_status='rolled_back'`；
 *  - 必须核对回来的 `annotation_id` 就是刚请求的那一条（否则删的可能是别的线段，
 *    界面上却把这一条移除 —— 那就成了"图上少一条、服务端还在另一条"）。
 */
import { describe, expect, it } from 'vitest'

import {
  parseRevokeResponse,
  revokeUrl,
  SERVER_ANNOTATION_CONTRACT,
} from '../serverAnnotations'

const body = (overrides: Record<string, unknown> = {}) => ({
  ok: true,
  status: 'revoked',
  annotation_id: 91,
  action_id: 7,
  action_status: 'rolled_back',
  symbol: '600111',
  label: '支撑位',
  reason: '',
  revoked_at: '2026-10-29T03:00:00Z',
  ...overrides,
})

describe('revokeUrl：撤销请求的唯一构造点', () => {
  it('无 reason 时不挂空参数', () => {
    expect(revokeUrl(91)).toBe('/copilot/annotations/91')
  })

  it('reason 做 URL 编码并去掉首尾空白', () => {
    expect(revokeUrl(91, '  点错价位了  ')).toBe(
      `/copilot/annotations/91?reason=${encodeURIComponent('点错价位了')}`,
    )
  })

  it('非正整数 id 直接拒绝（不拼出 /annotations/NaN 这种请求）', () => {
    expect(() => revokeUrl(0)).toThrow(/正整数/)
    expect(() => revokeUrl(Number.NaN)).toThrow(/正整数/)
  })

  it('契约事实：撤销接口存在且 undoSupported 为 true', () => {
    expect(SERVER_ANNOTATION_CONTRACT.revokePath).toBe('DELETE /copilot/annotations/{id}')
    expect(SERVER_ANNOTATION_CONTRACT.undoSupported).toBe(true)
  })
})

describe('parseRevokeResponse：fail-closed', () => {
  it('合法响应 → 标注 id / 动作 id 都可溯源', () => {
    const parsed = parseRevokeResponse(body(), 91)
    expect(parsed.ok).toBe(true)
    if (parsed.ok) {
      expect(parsed.value.annotationId).toBe(91)
      expect(parsed.value.actionId).toBe(7)
      expect(parsed.value.label).toBe('支撑位')
    }
  })

  it('回到的 annotation_id 与请求的不一致 → 拒绝（不许拿别的 id 当"已删"）', () => {
    const parsed = parseRevokeResponse(body({ annotation_id: 92 }), 91)
    expect(parsed.ok).toBe(false)
    if (!parsed.ok) expect(parsed.reason).toBe('mismatch')
  })

  it('status 不是 revoked → 拒绝', () => {
    const parsed = parseRevokeResponse(body({ status: 'applied' }), 91)
    expect(parsed.ok).toBe(false)
    if (!parsed.ok) expect(parsed.reason).toBe('not_revoked')
  })

  it('ok 不是 true → 拒绝', () => {
    const parsed = parseRevokeResponse(body({ ok: false }), 91)
    expect(parsed.ok).toBe(false)
  })

  it('动作终态不是 rolled_back → 拒绝（动作没转终态说明撤销没走完）', () => {
    const parsed = parseRevokeResponse(body({ action_status: 'applied' }), 91)
    expect(parsed.ok).toBe(false)
  })

  it('缺 action_id / annotation_id → 拒绝', () => {
    expect(parseRevokeResponse(body({ action_id: null }), 91).ok).toBe(false)
    expect(parseRevokeResponse(body({ annotation_id: null }), 91).ok).toBe(false)
  })

  it('顶层不是对象 → 拒绝', () => {
    expect(parseRevokeResponse(null, 91).ok).toBe(false)
    expect(parseRevokeResponse([body()], 91).ok).toBe(false)
  })
})
