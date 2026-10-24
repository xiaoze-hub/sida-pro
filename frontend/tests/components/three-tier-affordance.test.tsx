// @vitest-environment jsdom
// 入口可发现性(2026-10-24 小白可用性): 「铁三角共振」曾是纯静态标签 ——
// 用户不知道它是「数智决策」、也不知道能点。本文件守三件事:
//   1) 命名统一冠「数智决策」(与首页面板/机会页同源, 同一概念不再四个名字);
//   2) 有帮助 tooltip(小白不知道 GS趋势/机构活跃度是什么);
//   3) 结论可点且有箭头暗示, 点击进全市场共振页(深层链接 ?tab=resonance)。
import { cleanup, render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@panwatch/api', () => ({
  fetchAPI: (...a: unknown[]) => mocks.fetchAPI(...a),
}))

import { ThreeTierVerdictRow } from '@/components/ThreeTierVerdictRow'

function routeAll() {
  mocks.fetchAPI.mockImplementation((path: string) => {
    if (path.startsWith('/market/phase')) {
      return Promise.resolve({ current_phase: { phase: 'ferment', label: '发酵' } })
    }
    if (path.startsWith('/catalyst/calendar')) return Promise.resolve({ calendar: [] })
    if (path.includes('/wyckoff')) return Promise.resolve({ verdict: { stage: 'unknown' } })
    if (path.includes('/summary')) return Promise.resolve({ cancel_order_intent: { available: false } })
    if (path.startsWith('/decision-pioneer/')) {
      return Promise.resolve({
        resonance_state: '共振态',
        resonance_action: '仅作风控提示, 不构成买卖指令',
        resonance_warning: 'opportunity',
      })
    }
    return Promise.resolve({})
  })
}

/** 捕获 MemoryRouter 当前 location, 用于断言点击后的跳转目标。 */
let lastHref = ''
function LocationProbe() {
  const loc = useLocation()
  lastHref = `${loc.pathname}${loc.search}`
  return null
}

function renderRow() {
  lastHref = ''
  render(
    <MemoryRouter initialEntries={['/quote/600519']}>
      <ThreeTierVerdictRow symbol="600519" market="cn" />
      <Routes>
        <Route path="*" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ThreeTierVerdictRow 入口可发现性', () => {
  afterEach(() => {
    cleanup()
    mocks.fetchAPI.mockReset()
  })

  it('铁三角段冠名「数智决策」, 不再是裸「铁三角共振」', async () => {
    routeAll()
    renderRow()
    expect(await screen.findByText('数智决策·铁三角:')).toBeTruthy()
    expect(screen.queryByText('铁三角共振:')).toBeNull()
  })

  it('有帮助 tooltip 解释三个指标是什么(小白可读)', async () => {
    routeAll()
    renderRow()
    await screen.findByText('共振态')
    const help = screen.getByLabelText('什么是数智决策·铁三角')
    const tip = help.parentElement?.getAttribute('title') ?? ''
    expect(tip).toContain('GS趋势')
    expect(tip).toContain('AI机构活跃度')
    expect(tip).toContain('L2主力净流入')
  })

  it('点击共振结论跳转机会页共振页签(深层链接 ?tab=resonance)', async () => {
    routeAll()
    renderRow()
    const state = await screen.findByText('共振态')
    fireEvent.click(state.closest('button') as HTMLElement)
    expect(lastHref).toBe('/opportunities?tab=resonance')
  })
})
