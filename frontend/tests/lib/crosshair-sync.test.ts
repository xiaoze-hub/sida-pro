/**
 * 同页多图十字线联动(2026-09-20)。
 * 关键判据: ① 广播不回到自己(否则自己动一下又"联动"自己); ② 应用远端位置时不再次广播(防回环);
 * ③ 卸载必须反注册(否则跨页残留, 新页面一动鼠标就带着上一页的图跑)。
 */
import { describe, expect, it } from 'vitest'
import { __subscriberCount, broadcastCrosshair, registerCrosshair } from '@panwatch/biz-ui/lib/crosshair-sync'

describe('crosshair-sync', () => {
  it('注册后互相收到广播, 但**跳过自己**', () => {
    const gotA: unknown[] = []
    const gotB: unknown[] = []
    const a = { apply: (p: { time: unknown }) => gotA.push(p.time), clear: () => gotA.push(null) }
    const b = { apply: (p: { time: unknown }) => gotB.push(p.time), clear: () => gotB.push(null) }
    const offA = registerCrosshair(a)
    const offB = registerCrosshair(b)
    try {
      broadcastCrosshair({ time: 123, price: 1 }, a)
      expect(gotB).toEqual([123])
      expect(gotA).toEqual([]) // 自己不该收到
      broadcastCrosshair(null, b)
      expect(gotA).toEqual([null]) // 清空也要联动
    } finally {
      offA()
      offB()
    }
  })

  it('应用远端位置期间不再广播(防回环震荡)', () => {
    const seen: unknown[] = []
    const a = { apply: () => {}, clear: () => {} }
    const b = {
      apply: (_p: { time: unknown }) => {
        // 模拟"收到远端后自己也会触发一次 move" —— 此时再广播必须被忽略
        broadcastCrosshair({ time: 'loop', price: 1 }, b)
        seen.push('applied')
      },
      clear: () => {},
    }
    const offA = registerCrosshair(a)
    const offB = registerCrosshair(b)
    try {
      broadcastCrosshair({ time: 1, price: 1 }, a)
      expect(seen).toEqual(['applied'])
    } finally {
      offA()
      offB()
    }
  })

  it('卸载后不再收到广播(不跨页残留)', () => {
    const before = __subscriberCount()
    const off = registerCrosshair({ apply: () => {}, clear: () => {} })
    expect(__subscriberCount()).toBe(before + 1)
    off()
    expect(__subscriberCount()).toBe(before)
  })
})
