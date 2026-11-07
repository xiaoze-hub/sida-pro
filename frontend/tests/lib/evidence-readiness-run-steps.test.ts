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
  it('轨迹未断言成功或失败时，维度保持未确认', () => {
    const out = applyRunSteps(state(UNKNOWN_13), [
      { tool: 'get_stock_chips' },
      { tool: 'get_stock_quote', ok: null },
    ])
    expect(out.items.find((item) => item.key === 'get_stock_chips')?.status).toBe('unknown')
    expect(out.items.find((item) => item.key === 'get_stock_quote')?.status).toBe('unknown')
  })
  it('同一工具有失败和未确认结果时，不能断言全部失败', () => {
    const out = applyRunSteps(state(UNKNOWN_13), [
      { tool: 'get_stock_chips', ok: false },
      { tool: 'get_stock_chips', ok: null },
    ])
    expect(out.items.find((item) => item.key === 'get_stock_chips')?.status).toBe('unknown')
  })
  it('轨迹返回仅标记 returned，不等于确认数据有效', () => {
    const out = applyRunSteps(state(UNKNOWN_13), [
      { tool: 'get_stock_quote', ok: true },
      { tool: 'get_technical_analysis', ok: true },
      { tool: 'get_stock_chips', ok: true },
      { tool: 'get_main_intent', ok: true },
    ])
    expect(out.done).toBe(0)
    expect(out.unknown).toBe(0)
    expect(out.items.every((i) => i.status === 'returned')).toBe(true)
  })

  it('轨迹里失败的工具标 failed，不并进已取', () => {
    const out = applyRunSteps(state(UNKNOWN_13), [
      { tool: 'get_stock_chips', ok: false },
      { tool: 'get_stock_quote', ok: true },
    ])
    expect(out.items.find((i) => i.key === 'get_stock_chips')?.status).toBe('failed')
    expect(out.failed).toBe(1)
    expect(out.done).toBe(0)
    expect(out.items.find((i) => i.key === 'get_stock_quote')?.status).toBe('returned')
  })

  it('同一工具多次调用：任一返回则为 returned，不证明数据有效', () => {
    const out = applyRunSteps(state(UNKNOWN_13), [
      { tool: 'get_stock_chips', ok: false },
      { tool: 'get_stock_chips', ok: true },
    ])
    expect(out.items.find((i) => i.key === 'get_stock_chips')?.status).toBe('returned')
    expect(out.done).toBe(0)
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
