// @vitest-environment jsdom
/**
 * `usePausedInterval` 钉子 —— 2026-10-15(keep-alive 成本闸门补全)。
 *
 * 本 hook 是「裸 `window.setInterval` 轮询」的统一替代品, 守四件事:
 *   ① 活动页签: 挂载即回调一次(等价原 effect 首拉) + 之后按周期回调;
 *   ② 隐藏页签: **一个回调都不发**(隐藏着挂载、活动后隐藏、到点 都不发);
 *   ③ 隐藏 → 回前台: 立即补一次 + 恢复定时;
 *   ④ `immediate:false`(裸 setInterval, 无首拉) 与 `delayMs<=0`(停用) 两个开关语义。
 */
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { TabActiveProvider, usePausedInterval } from '@sida/base-ui'

let calls = 0

function Probe({
  immediate = true,
  delayMs = 40,
  dep = 0,
}: {
  immediate?: boolean
  delayMs?: number
  dep?: number
}) {
  // 回调每次渲染重造 —— 必须**不**导致重装定时器/额外请求(靠 ref)。
  usePausedInterval(() => {
    calls += 1
  }, delayMs, { immediate, deps: [dep] })
  return null
}

const advance = (ms: number) => act(() => void vi.advanceTimersByTime(ms))

beforeEach(() => {
  calls = 0
  vi.useFakeTimers()
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('① 活动页签: 挂载补一次 + 定时', () => {
  it('挂载即回调一次, 之后每 delayMs 一次', () => {
    render(<Probe />)
    expect(calls).toBe(1) // 首拉
    advance(120)
    expect(calls).toBe(4) // 1 + (40/80/120 三拍)
  })
})

describe('② 隐藏页签: 彻底停表', () => {
  it('隐藏着挂载 → 一个回调都不发; 回前台立即补一次并恢复定时', () => {
    const { rerender } = render(
      <TabActiveProvider active={false}>
        <Probe />
      </TabActiveProvider>,
    )
    advance(500)
    expect(calls).toBe(0) // 隐藏: 挂载/到点 都不发

    rerender(
      <TabActiveProvider active>
        <Probe />
      </TabActiveProvider>,
    )
    expect(calls).toBe(1) // 回前台补一次
    advance(120)
    expect(calls).toBe(4) // 定时恢复
  })

  it('活动 → 隐藏: 时间推进不再回调; 回前台补一次', () => {
    const { rerender } = render(
      <TabActiveProvider active>
        <Probe />
      </TabActiveProvider>,
    )
    expect(calls).toBe(1)
    advance(120)
    const baseline = calls
    expect(baseline).toBe(4)

    rerender(
      <TabActiveProvider active={false}>
        <Probe />
      </TabActiveProvider>,
    )
    advance(1000)
    expect(calls).toBe(baseline) // 隐藏期间: 表停了, 一个都不加

    rerender(
      <TabActiveProvider active>
        <Probe />
      </TabActiveProvider>,
    )
    expect(calls).toBe(baseline + 1) // 补一次
    advance(120)
    expect(calls).toBe(baseline + 1 + 3) // 定时恢复
  })
})

describe('③ immediate=false: 裸 setInterval 语义(无首拉), 但回前台仍补一次', () => {
  it('挂载不补, 只等第一拍; 隐藏 → 回前台仍补一次', () => {
    const { rerender } = render(
      <TabActiveProvider active>
        <Probe immediate={false} />
      </TabActiveProvider>,
    )
    expect(calls).toBe(0) // 无首拉
    advance(40)
    expect(calls).toBe(1)

    rerender(
      <TabActiveProvider active={false}>
        <Probe immediate={false} />
      </TabActiveProvider>,
    )
    advance(500)
    const baseline = calls

    rerender(
      <TabActiveProvider active>
        <Probe immediate={false} />
      </TabActiveProvider>,
    )
    expect(calls).toBe(baseline + 1) // 从停用恢复: 仍然补一次
  })
})

describe('④ delayMs<=0: 停用(不打表也不回调)', () => {
  it('停用时零回调; 转为正数(启用)时补一次并开始计时', () => {
    const { rerender } = render(
      <TabActiveProvider active>
        <Probe delayMs={0} />
      </TabActiveProvider>,
    )
    advance(500)
    expect(calls).toBe(0)

    rerender(
      <TabActiveProvider active>
        <Probe delayMs={40} />
      </TabActiveProvider>,
    )
    expect(calls).toBe(1) // 启用即补一次
    advance(120)
    expect(calls).toBe(4)
  })
})

describe('⑤ deps: 变化即重挂(立即补一次 + 重新计时)', () => {
  it('dep 变化 → 立即补一次', () => {
    const { rerender } = render(
      <TabActiveProvider active>
        <Probe dep={1} />
      </TabActiveProvider>,
    )
    expect(calls).toBe(1)

    rerender(
      <TabActiveProvider active>
        <Probe dep={2} />
      </TabActiveProvider>,
    )
    expect(calls).toBe(2) // 依赖变化重挂 → 补一次
  })
})
