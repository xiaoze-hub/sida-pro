// @vitest-environment jsdom
/** 提案审核台独立页：薄壳不改逻辑，但「采纳≠生效」这句诚信提示必须上屏。 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

vi.mock('@/components/copilot/ProposalPanel', () => ({
  ProposalPanel: () => <div data-testid="proposal-panel">panel</div>,
}))

import ProposalsPage from '@/pages/copilot/ProposalsPage'

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(cleanup)

describe('ProposalsPage', () => {
  it('渲染标题与提案面板', () => {
    render(<ProposalsPage />)
    expect(screen.getByText('能力提案')).toBeTruthy()
    expect(screen.getByTestId('proposal-panel')).toBeTruthy()
  })

  it('明确写出「采纳只改状态，不改配方」——不允许用文案暗示已生效', () => {
    render(<ProposalsPage />)
    expect(screen.getByText(/采纳只改状态，不改配方/)).toBeTruthy()
    expect(screen.getByText(/采纳后的能力不会立刻出现在回答里/)).toBeTruthy()
  })
})
