// @vitest-environment jsdom
/**
 * 结构化证据区（2026-11-03）：`AnswerCard` 只如实展示后端给的东西。
 *
 * 钉住四条会被人顺手"优化"掉的诚实约束：
 *  1. `evidence` 空/未传 → 整区不渲染（不许补「证据 0 条」或一条假的「全部取到」）；
 *  2. `status === 'missing'` 用降级样式并写「未取到」，其余原样 `ok`；
 *  3. `unverified` 非空 → 明写「N 个数字未通过对账」，数值带不可复制的弱化标记，答案不删改；
 *  4. 「多维依据透视」正名成「证据分组（单模型文本归类）」，testid 不变（防误读成多智能体）。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import { AnswerCard } from '@/components/copilot/AnswerCard'
import { pickEvidence, pickUnverified, settleStreamBubble } from '@/pages/AiCopilot'
import { MultiDimensionEvidencePanel } from '@/components/copilot/MultiAgentDebatePanel'
import { parseAnswer } from '@/lib/copilot/answer-blocks'
import type { EvidenceItem } from '@/lib/copilot/types'

const RAW = `【结论】缩量回踩未破位，维持观察
【依据】
- 主力净流入 1.2 亿
【口径】通达信实时
以上分析不构成投资建议。`

const ITEM = (over: Partial<EvidenceItem>): EvidenceItem => ({
  evidence_id: 'run-1:1',
  tool: 'get_chip',
  source: 'TDX',
  as_of: '2026-11-03 14:30',
  caliber: 'default',
  status: 'ok',
  digest: '筹码集中度 12.3%',
  run_id: 'run-1',
  ...over,
})

beforeEach(() => localStorage.setItem('token', `e30.${btoa(JSON.stringify({ sub: 'test-user' }))}.signature`))

afterEach(() => {
  localStorage.removeItem('token')
  cleanup()
  sessionStorage.clear()
})

describe('AnswerCard 结构化证据区', () => {
  it('没有 evidence 时整区不渲染（不写 0 条、不编造「全部取到」）', () => {
    render(<AnswerCard parsed={parseAnswer(RAW)} />)
    expect(screen.queryByTestId('copilot-answer-provenance')).toBeNull()

    cleanup()
    render(<AnswerCard parsed={parseAnswer(RAW)} evidence={[]} />)
    expect(screen.queryByTestId('copilot-answer-provenance')).toBeNull()
  })

  it('列出 工具 · 时点 · 状态，missing 行降级并写「未取到」', () => {
    render(
      <AnswerCard
        parsed={parseAnswer(RAW)}
        evidence={[
          ITEM({}),
          ITEM({ evidence_id: 'run-1:2', tool: 'get_l2', as_of: null, status: 'missing' }),
        ]}
      />,
    )

    const items = screen.getAllByTestId('copilot-evidence-item')
    expect(items).toHaveLength(2)
    expect(items[0].getAttribute('data-status')).toBe('ok')
    expect(items[1].getAttribute('data-status')).toBe('missing')
    expect(items[0].textContent).toContain('已返回（未核验）')

    // 时点缺失显示 --，不补今日
    expect(screen.getAllByText('--（基准日未确认）')).toHaveLength(2)
    // missing 行写「未取到」，且工具名走弱化（删除线）
    expect(screen.getByText('未取到')).toBeTruthy()
    expect(screen.getByText('get_l2').className).toContain('line-through')
  })

  it('旧默认元数据和未知来源、口径明确显示未确认', () => {
    render(<AnswerCard parsed={parseAnswer(RAW)} evidence={[ITEM({})]} />)
    const item = screen.getByTestId('copilot-evidence-item')
    expect(item.textContent).toContain('来源 未确认 · 口径 未确认')
    expect(item.textContent).toContain('基准日未确认')
    expect(item.textContent).not.toContain('2026-11-03 14:30')
  })

  it('未对账数字写明条数并加不可复制的弱化标记，答案正文不隐藏', () => {
    render(
      <AnswerCard
        parsed={parseAnswer(RAW)}
        evidence={[ITEM({})]}
        unverified={[{ value: '1.2 亿', kind: 'amount', context: '主力净流入' }]}
      />,
    )

    expect(screen.getByText('1 个数字未通过对账')).toBeTruthy()
    const mark = screen.getByTestId('copilot-unverified-number')
    expect(mark.textContent).toBe('1.2 亿')
    expect(mark.className).toContain('select-none')
    // 答案正文仍在（不因为未对账就摘掉结论）
    expect(screen.getByText(/缩量回踩未破位/)).toBeTruthy()
  })

  it('未对账为空时不出现标记区（不写「0 个未对账」）', () => {
    render(<AnswerCard parsed={parseAnswer(RAW)} evidence={[ITEM({})]} unverified={[]} />)
    expect(screen.queryByTestId('copilot-evidence-unverified')).toBeNull()
    expect(screen.queryByText(/未通过对账/)).toBeNull()
  })
})

describe('多维依据透视正名', () => {
  it('标题写「证据分组（单模型文本归类）」，testid 保持 copilot-multi-dimension-evidence', () => {
    render(
      <MultiDimensionEvidencePanel
        perspectives={[{ role: 'technical', agentName: '技术面分析', status: '中性', conclusion: '缩量回踩' }]}
      />,
    )
    expect(screen.getByTestId('copilot-multi-dimension-evidence')).toBeTruthy()
    expect(screen.getByText('证据分组（单模型文本归类） (1 组)')).toBeTruthy()
    expect(screen.queryByText(/多维依据透视/)).toBeNull()
  })
})

describe('done 载荷落库（pick* / settleStreamBubble）', () => {
  it('pickEvidence 只认后端字段，时点空串归一成 null，状态认不出就丢（不猜成 ok）', () => {
    expect(
      pickEvidence([
        {
          evidence_id: 'r:1', tool: 'get_chip', source: 'TDX', as_of: '',
          caliber: 'default', status: 'ok', digest: 'x', run_id: 'r',
        },
      ]),
    ).toEqual([
      {
        evidence_id: 'r:1', tool: 'get_chip', source: 'TDX', as_of: null,
        caliber: 'default', status: 'ok', digest: 'x', run_id: 'r',
      },
    ])
    expect(pickEvidence([{ tool: 'get_chip', status: 'weird' }])).toBeUndefined()
    expect(pickEvidence(undefined)).toBeUndefined()
    expect(pickEvidence([])).toBeUndefined()
  })

  it('pickUnverified 只收结构化条目，老的字符串形状一条都不收', () => {
    expect(
      pickUnverified({ unverified: [{ value: '1.2 亿', kind: 'amount', context: '主力净流入' }, '1.2亿'] }),
    ).toEqual([{ value: '1.2 亿', kind: 'amount', context: '主力净流入' }])
    expect(pickUnverified(undefined)).toBeUndefined()
    expect(pickUnverified({ rows: [] })).toBeUndefined()
  })

  it('settleStreamBubble 把 evidence / unverified 落到气泡上，没给就清空（不继承上一轮）', () => {
    const item = {
      evidence_id: 'r:1', tool: 'get_chip', source: 'TDX', as_of: '2026-11-03 14:30',
      caliber: 'default', status: 'ok' as const, digest: 'd', run_id: 'r',
    }
    const first = settleStreamBubble([], '正文', false, 'r', null, [item], [{ value: '9.9', kind: 'k', context: 'c' }])
    expect(first[0].evidence).toHaveLength(1)
    expect(first[0].unverified).toHaveLength(1)

    const second = settleStreamBubble(first, '正文二', false, 'r2', null, null, null)
    expect(second[0].evidence).toBeUndefined()
    expect(second[0].unverified).toBeUndefined()
  })
})
