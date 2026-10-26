import { describe, expect, it } from 'vitest'

import { isTerminalStatus, progressText, type RunStatus } from '@/lib/copilot/jobs'

/**
 * 后台执行进度文案（纯函数）。
 *
 * 关键纪律：**不编百分比、不把"还没开始"写成 0 步**。`tool_calls=0` 是"跑过但没调工具"，
 * `tool_calls=null` 是"还没数据" —— 两者在 UI 上必须不同，否则用户会以为在跑。
 */
describe('progressText', () => {
  it('无状态时是"已提交，等待执行"，不是 0 步', () => {
    expect(progressText(null)).toBe('已提交，等待执行…')
    expect(progressText(null)).not.toContain('0 步')
  })

  it('queued 不显示步数（还没开始跑）', () => {
    const s: RunStatus = { status: 'queued', tool_calls: null }
    expect(progressText(s)).toContain('排队')
    expect(progressText(s)).not.toContain('0 步')
  })

  it('running 显示步数，但 tool_calls 为 null 时不写 0', () => {
    expect(progressText({ status: 'running', tool_calls: 3 })).toContain('3 步')
    expect(progressText({ status: 'running', tool_calls: null })).toBe('后台执行中…')
  })

  it('running + tool_calls=0 明确写 0 步（真跑过但没调工具）', () => {
    expect(progressText({ status: 'running', tool_calls: 0 })).toContain('0 步')
  })

  it('ok 显示步数与耗时', () => {
    const t = progressText({ status: 'ok', tool_calls: 3, elapsed_ms: 8179 })
    expect(t).toContain('已完成')
    expect(t).toContain('3 步')
    expect(t).toContain('8.2s')
  })

  it('degraded 明说部分失败', () => {
    expect(progressText({ status: 'degraded', tool_calls: 2 })).toContain('部分工具失败')
  })

  it('failed 把服务端 error 原文透出来，不吞成"出错了"', () => {
    const t = progressText({ status: 'failed', error: 'RuntimeError: 模型网关 502' })
    expect(t).toContain('执行失败')
    expect(t).toContain('模型网关 502')
  })

  it('failed 且无 error 时不编原因', () => {
    expect(progressText({ status: 'failed', error: null })).toBe('执行失败')
  })

  it('cancelled 明确说取消', () => {
    expect(progressText({ status: 'cancelled' })).toBe('已取消')
  })

  it('未知状态原样透出，不猜', () => {
    expect(progressText({ status: 'weird' })).toBe('状态：weird')
  })

  it('不出现百分比（没有分母，假装进度就是假数据）', () => {
    for (const st of ['queued', 'running', 'ok', 'degraded', 'failed', 'cancelled']) {
      expect(progressText({ status: st, tool_calls: 3, elapsed_ms: 5000 })).not.toContain('%')
    }
  })
})

describe('isTerminalStatus', () => {
  it('终态集合与后端一致（含历史词汇 ok/degraded/error）', () => {
    for (const s of ['ok', 'degraded', 'error', 'failed', 'succeeded', 'cancelled']) {
      expect(isTerminalStatus(s)).toBe(true)
    }
    for (const s of ['queued', 'running', 'paused', '', null, undefined]) {
      expect(isTerminalStatus(s as string)).toBe(false)
    }
  })
})
