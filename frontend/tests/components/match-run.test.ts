import { describe, expect, it } from 'vitest'

import { matchRunForMessage } from '@/lib/copilot/match-run'
import type { CopilotRunRow } from '@/lib/copilot/api'

const run = (id: string, q: string): CopilotRunRow => ({
  run_id: id,
  conversation_id: 1,
  recipe: null,
  status: 'ok',
  tool_calls: 3,
  elapsed_ms: 1000,
  question: q,
  created_at: '2026-10-25T09:00:00',
})

describe('matchRunForMessage（答案 ↔ 工具轮次对齐）', () => {
  const msgs = [
    { role: 'user', content: '上证指数自七月到十月趋势下跌的原因' },
    { role: 'assistant', content: '回答一' },
    { role: 'user', content: '两融余额现在多少' },
    { role: 'assistant', content: '回答二' },
  ]

  it('按提问原文对齐：第一条答案对第一轮，第二条对第二轮', () => {
    const runs = [
      run('r1', '上证指数自七月到十月趋势下跌的原因'),
      run('r2', '两融余额现在多少'),
    ]
    expect(matchRunForMessage(msgs, runs, 1)?.run_id).toBe('r1')
    expect(matchRunForMessage(msgs, runs, 3)?.run_id).toBe('r2')
  })

  it('前后空白不影响匹配（历史消息常带换行）', () => {
    const runs = [run('r1', '  上证指数自七月到十月趋势下跌的原因\n')]
    expect(matchRunForMessage(msgs, runs, 1)?.run_id).toBe('r1')
  })

  it('匹配不到就返回 null —— 宁可没有，也不能挂错工具链', () => {
    const runs = [run('r9', '完全不相干的问题')]
    expect(matchRunForMessage(msgs, runs, 1)).toBeNull()
  })

  it('用户消息是助手消息（下标错位）时返回 null', () => {
    expect(matchRunForMessage(msgs, [run('r1', 'x')], 0)).toBeNull()
  })

  it('前面没有用户消息（如系统提示开头）返回 null', () => {
    const only = [{ role: 'assistant', content: '开场白' }]
    expect(matchRunForMessage(only, [run('r1', 'x')], 0)).toBeNull()
  })

  it('runs 为空/缺失时安全返回 null', () => {
    expect(matchRunForMessage(msgs, [], 1)).toBeNull()
    expect(matchRunForMessage(msgs, undefined as never, 1)).toBeNull()
  })

  it('同一问题有多次运行（重试）时取第一条（列表已按时间倒序）', () => {
    const runs = [
      run('newest', '两融余额现在多少'),
      run('older', '两融余额现在多少'),
    ]
    expect(matchRunForMessage(msgs, runs, 3)?.run_id).toBe('newest')
  })

  it('用户消息带 @ 提及前缀也能匹配（输入框支持 @参谋）', () => {
    const m = [
      { role: 'user', content: '@参谋 上证指数自七月到十月趋势下跌的原因' },
      { role: 'assistant', content: '答' },
    ]
    const runs = [run('r1', '上证指数自七月到十月趋势下跌的原因')]
    expect(matchRunForMessage(m, runs, 1)?.run_id).toBe('r1')
  })

  it('多余空白/换行不影响（历史消息会重排空白）', () => {
    const m = [
      { role: 'user', content: '两融余额   现在\n多少' },
      { role: 'assistant', content: '答' },
    ]
    const runs = [run('r1', '两融余额 现在 多少')]
    expect(matchRunForMessage(m, runs, 1)?.run_id).toBe('r1')
  })

  it('一端被截断时用前缀兜底（后端 question 可能截到 N 字）', () => {
    const m = [
      { role: 'user', content: '上证指数自七月到十月趋势下跌的原因是什么，帮我从宏观和资金面拆解' },
      { role: 'assistant', content: '答' },
    ]
    const runs = [run('r1', '上证指数自七月到十月趋势下跌的原因')]
    expect(matchRunForMessage(m, runs, 1)?.run_id).toBe('r1')
  })

  it('问题太短(不足8字)时不做前缀兜底，避免误挂', () => {
    const m = [
      { role: 'user', content: '涨了吗' },
      { role: 'assistant', content: '答' },
    ]
    const runs = [run('r1', '涨了')]
    expect(matchRunForMessage(m, runs, 1)).toBeNull()
  })

  it('中间夹着别的助手消息也不错位（找最近的那条 user）', () => {
    const odd = [
      { role: 'user', content: 'A' },
      { role: 'assistant', content: 'a1' },
      { role: 'assistant', content: 'a2' },
    ]
    const runs = [run('ra', 'A')]
    expect(matchRunForMessage(odd, runs, 2)?.run_id).toBe('ra')
  })
})
