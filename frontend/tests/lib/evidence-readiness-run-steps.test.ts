/**
 * 轨迹补全齐套度（2026-10-31）。
 *
 * 线上症状：`已取 0/13 · 未确认 13` 而正文写满数据 —— SSE 没有完成帧，
 * 但服务端逐工具记了 ok（CopilotStep.ok）。这里钉住"怎么用轨迹补、以及不许乱补"。
 */
import { describe, expect, it } from 'vitest'

import { applyRunSteps, type ReadinessItem, type ReadinessState } from '@/lib/copilot/evidence-readiness'

function state(items: ReadinessItem[]): ReadinessState {
  const done = items.filter((i) => i.status === 'ok').length
  const failed = items.filter((i) => i.status === 'failed').length
  const unknown = items.filter((i) => i.status === 'unknown').length
  return { items, done, total: items.length, failed, unknown }
}

const UNKNOWN_13: ReadinessItem[] = [
  { key: 'get_stock_quote', label: '行情', group: '走势', status: 'unknown' },
  { key: 'get_technical_analysis', label: '技术面', group: '走势', status: 'unknown' },
  { key: 'get_stock_chips', label: '筹码', group: '筹码', status: 'unknown' },
  { key: 'get_main_intent', label: '主力', group: '主力', status: 'unknown' },
]

describe('applyRunSteps', () => {
  it('轨迹里成功的工具把 unknown 补成 ok（13 未确认 → 有据可查）', () => {
    const out = applyRunSteps(state(UNKNOWN_13), [
      { tool: 'get_stock_quote', ok: true },
      { tool: 'get_technical_analysis', ok: true },
      { tool: 'get_stock_chips', ok: true },
      { tool: 'get_main_intent', ok: true },
    ])
    expect(out.done).toBe(4)
    expect(out.unknown).toBe(0)
    expect(out.items.every((i) => i.status === 'ok')).toBe(true)
  })

  it('轨迹里失败的工具标 failed，不并进已取', () => {
    const out = applyRunSteps(state(UNKNOWN_13), [
      { tool: 'get_stock_chips', ok: false },
      { tool: 'get_stock_quote', ok: true },
    ])
    expect(out.items.find((i) => i.key === 'get_stock_chips')?.status).toBe('failed')
    expect(out.failed).toBe(1)
    expect(out.done).toBe(1)
  })

  it('同一工具多次调用：任一成功即 ok（多标的扫描单只失败不算整维度没数据）', () => {
    const out = applyRunSteps(state(UNKNOWN_13), [
      { tool: 'get_stock_chips', ok: false },
      { tool: 'get_stock_chips', ok: true },
    ])
    expect(out.items.find((i) => i.key === 'get_stock_chips')?.status).toBe('ok')
  })

  it('轨迹里没有的工具保持 unknown（不猜成功）', () => {
    const out = applyRunSteps(state(UNKNOWN_13), [{ tool: 'get_stock_quote', ok: true }])
    expect(out.items.filter((i) => i.status === 'unknown').length).toBe(3)
  })

  it('已有结论不被轨迹覆盖', () => {
    const s = state([
      { key: 'get_stock_chips', label: '筹码', group: '筹码', status: 'failed' },
    ])
    const out = applyRunSteps(s, [{ tool: 'get_stock_chips', ok: true }])
    expect(out.items[0].status).toBe('failed')
  })

  it('空轨迹/空条目原样返回（不多发请求也不改状态）', () => {
    const s = state(UNKNOWN_13)
    expect(applyRunSteps(s, null)).toBe(s)
    expect(applyRunSteps(s, [])).toBe(s)
  })
})
