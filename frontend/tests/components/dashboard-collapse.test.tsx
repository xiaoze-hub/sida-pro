// @vitest-environment jsdom
/**
 * 首页证据层折叠 —— 2026-10-24 首屏重排。
 *
 * 设计意图（产品裁决：首屏不是「数字 ≤N」，是「结论能独立读懂」）：
 *   ① 折叠 ≠ 藏。表头必须自带关键数字，收起状态也能得出结论。
 *   ② 收起时**不挂载正文** —— 面积图/列表不该为看不见的东西跑渲染。
 *   ③ 状态按块记，块与块互不影响；老用户的 `sida_dashboard_more=1` 迁移成「默认展开」。
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import CollapsibleSection from '@/components/dashboard/CollapsibleSection'
import {
  DASH_COLLAPSE_PREFIX,
  readCollapsed,
  writeCollapsed,
} from '@/lib/dashboard-collapse'

beforeEach(() => {
  try {
    localStorage.clear()
  } catch {
    /* ignore */
  }
})
afterEach(() => {
  cleanup()
  try {
    localStorage.clear()
  } catch {
    /* ignore */
  }
})

describe('readCollapsed / writeCollapsed', () => {
  it('未存过时按默认值', () => {
    expect(readCollapsed('fundflow', true)).toBe(true)
    expect(readCollapsed('fundflow', false)).toBe(false)
  })

  it('存过就以存储为准', () => {
    writeCollapsed('fundflow', false)
    expect(readCollapsed('fundflow', true)).toBe(false)
    writeCollapsed('fundflow', true)
    expect(readCollapsed('fundflow', false)).toBe(true)
  })

  it('老用户点过「展开更多」（sida_dashboard_more=1）→ 各块默认展开，不突然全收起', () => {
    localStorage.setItem('sida_dashboard_more', '1')
    expect(readCollapsed('anomalies', true)).toBe(false)
    // 但显式存过的仍然优先
    localStorage.setItem(`${DASH_COLLAPSE_PREFIX}anomalies`, '1')
    expect(readCollapsed('anomalies', true)).toBe(true)
  })

  it('localStorage 抛异常时退回默认值，不炸渲染', () => {
    const orig = Storage.prototype.getItem
    Storage.prototype.getItem = () => {
      throw new Error('blocked')
    }
    try {
      expect(readCollapsed('x', true)).toBe(true)
    } finally {
      Storage.prototype.getItem = orig
    }
  })
})

describe('CollapsibleSection', () => {
  const tree = () => (
    <CollapsibleSection
      id="fundflow"
      header={<span>大盘资金流 +22.5亿</span>}
      defaultCollapsed
    >
      <div data-testid="heavy-body">面积图</div>
    </CollapsibleSection>
  )

  it('默认折叠：表头在，正文不挂载（不跑重渲染）', () => {
    render(tree())
    expect(screen.getByText('大盘资金流 +22.5亿')).toBeTruthy()
    expect(screen.queryByTestId('heavy-body')).toBeNull()
    expect(screen.getByTestId('evidence-fundflow').getAttribute('data-collapsed')).toBe('true')
  })

  it('点开展开正文，并把状态写进 localStorage', () => {
    render(tree())
    fireEvent.click(screen.getByTestId('evidence-toggle-fundflow'))
    expect(screen.getByTestId('heavy-body')).toBeTruthy()
    expect(localStorage.getItem(`${DASH_COLLAPSE_PREFIX}fundflow`)).toBe('0')
    expect(
      screen.getByTestId('evidence-toggle-fundflow').getAttribute('aria-expanded'),
    ).toBe('true')
  })

  it('重新挂载时读取上次状态（记忆生效）', () => {
    const { unmount } = render(tree())
    fireEvent.click(screen.getByTestId('evidence-toggle-fundflow'))
    unmount()
    render(tree())
    expect(screen.getByTestId('heavy-body')).toBeTruthy()
  })

  it('块与块互不影响：一个展开不会改另一个的键', () => {
    render(
      <>
        <CollapsibleSection id="a" header={<span>A</span>}>
          <i data-testid="body-a" />
        </CollapsibleSection>
        <CollapsibleSection id="b" header={<span>B</span>}>
          <i data-testid="body-b" />
        </CollapsibleSection>
      </>,
    )
    fireEvent.click(screen.getByTestId('evidence-toggle-a'))
    expect(screen.getByTestId('body-a')).toBeTruthy()
    expect(screen.queryByTestId('body-b')).toBeNull()
    expect(localStorage.getItem(`${DASH_COLLAPSE_PREFIX}b`)).toBeNull()
  })
})
