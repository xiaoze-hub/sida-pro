/**
 * 会话消息总线(2026-09-22)。
 * 判据: ① 同一消息 30s 内只累加计数(轮询型失败不许刷屏); ② 新消息在最前; ③ 容量上限;
 * ④ 订阅者立刻收到; ⑤ 清空后订阅者也知道。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  __resetAlerts,
  clearAlerts,
  getAlerts,
  pushAlert,
  subscribeAlerts,
} from '@panwatch/biz-ui/lib/alert-bus'

beforeEach(() => {
  __resetAlerts()
})

describe('pushAlert', () => {
  it('新消息排在最前', () => {
    pushAlert({ level: 'info', text: '第一条' })
    pushAlert({ level: 'warn', text: '第二条' })
    expect(getAlerts().map((a) => a.text)).toEqual(['第二条', '第一条'])
  })

  it('同一条消息在去重窗口内**只累加计数**, 不新增行(防轮询刷屏)', () => {
    pushAlert({ level: 'warn', text: 'GET /api/x 失败' })
    pushAlert({ level: 'warn', text: 'GET /api/x 失败' })
    pushAlert({ level: 'warn', text: 'GET /api/x 失败' })
    const list = getAlerts()
    expect(list).toHaveLength(1)
    expect(list[0].repeat).toBe(3)
  })

  it('去重键不同(同一文案不同路径) → 各自成行', () => {
    pushAlert({ level: 'warn', text: '失败', key: 'GET:/a' })
    pushAlert({ level: 'warn', text: '失败', key: 'GET:/b' })
    expect(getAlerts()).toHaveLength(2)
  })

  it('重复消息会**移到最前**(表示"现在又发生了")', () => {
    pushAlert({ level: 'info', text: 'A', key: 'a' })
    pushAlert({ level: 'info', text: 'B', key: 'b' })
    pushAlert({ level: 'info', text: 'A', key: 'a' })
    expect(getAlerts().map((a) => a.text)).toEqual(['A', 'B'])
    expect(getAlerts()[0].repeat).toBe(2)
  })

  it('容量上限 200(超出的丢掉最旧)', () => {
    for (let i = 0; i < 210; i++) pushAlert({ level: 'info', text: `m${i}`, key: `k${i}` })
    const list = getAlerts()
    expect(list).toHaveLength(200)
    expect(list[0].text).toBe('m209')
  })
})

describe('订阅 / 清空', () => {
  it('推送即通知订阅者; 退订后不再通知', () => {
    const fn = vi.fn()
    const off = subscribeAlerts(fn)
    pushAlert({ level: 'info', text: 'x' })
    expect(fn).toHaveBeenCalledTimes(1)
    off()
    pushAlert({ level: 'info', text: 'y' })
    expect(fn).toHaveBeenCalledTimes(1)
  })

  it('clearAlerts 清空且通知', () => {
    pushAlert({ level: 'info', text: 'x' })
    const fn = vi.fn()
    subscribeAlerts(fn)
    clearAlerts()
    expect(getAlerts()).toEqual([])
    expect(fn).toHaveBeenCalled()
  })
})
