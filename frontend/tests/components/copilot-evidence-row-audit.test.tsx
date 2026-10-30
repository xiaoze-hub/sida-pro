// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import EvidenceRowAuditPanel from '@/components/copilot/EvidenceRowAuditPanel'
import type { EvidenceRowAudit } from '@sida/api'

const audit = (rows: EvidenceRowAudit['rows'], total = 1): EvidenceRowAudit => ({
  basis: 'number_containment',
  rows,
  checked: 3,
  unverified_total: total,
  truncated: false,
})

describe('EvidenceRowAuditPanel', () => {
  it('只列有问题的行，并说明这是"本轮取数里未出现"而不是"编造"', () => {
    render(
      <EvidenceRowAuditPanel
        audit={audit([
          { index: 0, text: '实时行情 现价 9.65 元', claims: ['9.65元'], unverified: [] },
          { index: 1, text: '资金 主力净流入 1.23 亿', claims: ['1.23亿'], unverified: ['1.23亿'] },
        ])}
      />,
    )
    expect(screen.getByTestId('copilot-row-audit')).toBeTruthy()
    expect(screen.getByText(/1 个数字本轮取数里未出现/)).toBeTruthy()
    expect(screen.getByText(/1\.23亿/)).toBeTruthy()
    expect(screen.queryByText(/9\.65 元/)).toBeNull()
    expect(screen.getByText(/不判断来源归属，也不断言编造/)).toBeTruthy()
  })

  it('全部数字都对得上时不渲染任何东西（不摆"0 问题"装饰）', () => {
    const { container } = render(
      <EvidenceRowAuditPanel
        audit={audit([{ index: 0, text: '现价 9.65 元', claims: ['9.65元'], unverified: [] }], 0)}
      />,
    )
    expect(container.firstChild).toBeNull()
  })

  it('没有行时也不渲染', () => {
    const { container } = render(<EvidenceRowAuditPanel audit={audit([], 0)} />)
    expect(container.firstChild).toBeNull()
  })

  it('超长依据行截断展示，但 title 里保留全量原文', () => {
    const long = '维度'.repeat(60)
    render(<EvidenceRowAuditPanel audit={audit([{ index: 0, text: long, claims: ['1.0%'], unverified: ['1.0%'] }])} />)
    const cell = screen.getByText(new RegExp(`${'维度'.repeat(30)}`))
    expect(cell.getAttribute('title')).toBe(long)
  })

  it('截断提示的数字来自实际行数，不硬编码后端上限', () => {
    const rows = Array.from({ length: 5 }, (_, i) => ({
      index: i, text: `读数 ${i}.5 元`, claims: [`${i}.5元`], unverified: [`${i}.5元`],
    }))
    render(<EvidenceRowAuditPanel audit={{ ...audit(rows, 5), truncated: true }} />)
    expect(screen.getByText(/仅核对了前 5 行/)).toBeTruthy()
  })
})

describe('settleStreamBubble 带上对账结果', () => {
  it('终稿落库时把 done.evidence_audit 固化到气泡（下次渲染不用重新请求）', async () => {
    const { settleStreamBubble } = await import('@/pages/AiCopilot')
    const next = settleStreamBubble(
      [{ role: 'assistant', content: '半截' }],
      '完整正文',
      false,
      'run-1',
      audit([{ index: 0, text: '现价 9.65 元', claims: ['9.65元'], unverified: ['9.65元'] }]),
    )
    expect(next[0].runId).toBe('run-1')
    expect(next[0].evidenceAudit?.unverified_total).toBe(1)
  })

  it('没有 run 时不写关联，也不编造对账结果', async () => {
    const { settleStreamBubble } = await import('@/pages/AiCopilot')
    const next = settleStreamBubble([], '正文', false, null, null)
    expect(next[0].runId).toBeNull()
    expect(next[0].evidenceAudit).toBeNull()
  })
})
