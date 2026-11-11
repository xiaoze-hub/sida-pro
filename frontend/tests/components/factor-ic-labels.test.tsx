// @vitest-environment jsdom
/**
 * 因子有效性页 · 可读性(2026-11-11)。
 *
 * 用户反馈「因子有效性的因子是英文不知道是什么意思」, 本用例守三件事:
 *  ① 6 个因子都渲染中文名(**并保留**英文代号, 方便和后端日志对照);
 *  ② 未知 factor_code 不报错、原样显示(不猜、不塞"未知因子"占位);
 *  ③ 表下说明段存在, 且不出现绝对化承诺类措辞(这是研究页, 不是买卖指令)。
 *
 * 只测展示层: IC/IR 数值仍由后端给, 前端的 null→`--` 口径由 factor-ic.test.ts 守。
 */
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ evaluate: vi.fn(), history: vi.fn() }))

vi.mock('@sida/api', () => ({
  factorICApi: {
    evaluate: (...args: unknown[]) => api.evaluate(...args),
    history: (...args: unknown[]) => api.history(...args),
  },
}))

import FactorIC from '../../src/pages/FactorIC'

/** 后端 /recommendations/strategy-factor-ic 的形状(只取页面用到的字段) */
const row = (over: Partial<Record<string, unknown>> = {}) => ({
  ic: 0.031,
  ic_t: 1.82,
  ic_std: 0.052,
  ic_pooled: 0.024,
  ic_holdout: 0.018,
  ir: 0.6,
  sample_size: 412,
  ic_periods: 38,
  holdout_periods: 11,
  ...over,
})

const resp = {
  horizon: 5,
  days: 90,
  market: 'CN',
  holdout_ratio: 0.3,
  factors: {
    alpha_score: row({ ic: 0.041 }),
    catalyst_score: row({ ic: 0.022 }),
    quality_score: row({ ic: 0.017 }),
    risk_penalty: row({ ic: -0.028 }),
    crowd_penalty: row({ ic: -0.035 }),
    final_score: row({ ic: 0.046 }),
  },
}

/** 中文名 ←→ 代号(与后端 signal_explain.FACTOR_LABELS / FactorWeightsPanel 一致) */
const PAIRS: [string, string][] = [
  ['选股α', 'alpha_score'],
  ['催化', 'catalyst_score'],
  ['计划质量', 'quality_score'],
  ['风险', 'risk_penalty'],
  ['拥挤度', 'crowd_penalty'],
  ['综合得分', 'final_score'],
]

beforeEach(() => {
  api.evaluate.mockReset()
  api.history.mockReset()
  api.evaluate.mockResolvedValue(resp)
  api.history.mockResolvedValue({ horizon: 5, market: 'CN', days: 30, items: [] })
})

// 本仓 vitest 关了 globals, Testing Library 不会自动 cleanup —— 不手动清会串 DOM。
afterEach(() => {
  cleanup()
})

/** 等表格出现(首帧是 loading) */
async function renderTable() {
  render(<FactorIC />)
  await waitFor(() => expect(screen.getByRole('table')).toBeTruthy())
  return screen.getByRole('table')
}

describe('因子有效性页 · 因子中文化', () => {
  it('6 个因子行都显示中文名, 且英文代号仍保留在行内', async () => {
    const table = await renderTable()
    for (const [label, code] of PAIRS) {
      expect(within(table).getByText(label)).toBeTruthy()
      expect(within(table).getByText(code)).toBeTruthy()
    }
  })

  it('中文名与代号在同一个小格里(不是两列), 并带口径悬停说明', async () => {
    const table = await renderTable()
    const labelCell = within(table).getByText('选股α')
    const codeCell = within(table).getByText('alpha_score')
    // 同一个父节点 = 同一格
    expect(labelCell.parentElement).toBe(codeCell.parentElement)
    const title = labelCell.parentElement?.getAttribute('title') ?? ''
    expect(title).toContain('选股α')
    expect(title).toContain('alpha_score')
    expect(title.length).toBeGreaterThan(20)   // 有实际口径, 不是占位
  })

  it('摘要句也用中文名, 英文代号跟在括号里', async () => {
    render(<FactorIC />)
    await waitFor(() => expect(screen.getByRole('table')).toBeTruthy())
    // 最强因子是 final_score(IC 0.046)
    const line = screen.getByText(/最强/)
    expect(line.textContent).toContain('综合得分')
    expect(line.textContent).toContain('final_score')
  })

  it('未知 factor_code 不报错、原样显示代号', async () => {
    api.evaluate.mockResolvedValue({
      ...resp,
      factors: { ...resp.factors, mystery_factor_v2: row({ ic: 0.01 }) },
    })
    const table = await renderTable()
    const cell = within(table).getByText('mystery_factor_v2')
    expect(cell).toBeTruthy()
    // 未知代号不当成"没算出来": 该行 IC 照常显示
    expect(within(table).getByText('0.010')).toBeTruthy()
  })
})

describe('因子有效性页 · 指标怎么读', () => {
  it('表下有「这些指标怎么读」+「每个因子在看什么」两段说明', async () => {
    await renderTable()
    const legend = screen.getByTestId('factor-ic-metric-legend')
    const glossary = screen.getByTestId('factor-ic-factor-glossary')

    expect(legend.textContent).toContain('这些指标怎么读')
    expect(glossary.textContent).toContain('每个因子在看什么')

    // 8 个列名各有解释(IC/t/IR/样本外 IC/参考值/样本/期数/近 30 日)
    for (const term of ['IC', 't', 'IR', '样本外 IC', '参考值', '样本', '期数', '近 30 日']) {
      expect(legend.textContent).toContain(term)
    }
    // 每个因子的中文名都在说明里(与表里同一份映射)
    for (const [label] of PAIRS) {
      expect(glossary.textContent).toContain(label)
    }
  })

  it('IC 的解释交代"越接近 0 越弱", 参考值强调"只作对照"', async () => {
    await renderTable()
    const legend = screen.getByTestId('factor-ic-metric-legend')
    const t = legend.textContent ?? ''
    expect(t).toContain('越接近 0')
    expect(t).toContain('预测力越弱')
    expect(t).toContain('只作对照')
    expect(t).toContain('过拟合')
  })

  it('说明段不含绝对化承诺类措辞(研究页, 非买卖指令)', async () => {
    await renderTable()
    const legend = screen.getByTestId('factor-ic-metric-legend')
    const glossary = screen.getByTestId('factor-ic-factor-glossary')
    const text = `${legend.textContent ?? ''}${glossary.textContent ?? ''}`
    for (const banned of ['一定', '保证', '必然', '肯定', '稳赚', '必涨', '必跌', '包赚']) {
      expect(text).not.toContain(banned)
    }
  })
})
