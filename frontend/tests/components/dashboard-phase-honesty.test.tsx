// @vitest-environment jsdom
//
// 2026-09-14 首页走查 ①②③④ 的钉住用例。
// 铁律: 拉取失败 ≠ 没有数据。失败必须显式可见(不得塌成 '--'/'暂无'/永久"同步中"),
// 真无数据时必须透传后端 note 原文(不得本地编造原因)。
//
// 注: fetchAPI 用"状态驱动"的普通函数桩(非 vi.fn) —— 本仓 vitest 版本下
// beforeEach 里 reset 过的 vi.fn 抛出/拒绝会被记成测试失败, 与实现无关, 见本文件历史。
import { cleanup, render, renderHook, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ fail: null as string | null, resp: null as unknown }))
vi.mock('@panwatch/api', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>()
  return {
    ...actual,
    fetchAPI: () =>
      api.fail ? Promise.reject(new Error(api.fail)) : Promise.resolve(api.resp),
  }
})
// jsdom 无 canvas: 图表库只做桩(与 flow-history-chart.test.tsx 同法)
vi.mock('@panwatch/biz-ui/lib/echarts-core', () => ({
  default: { graphic: { LinearGradient: class {} } },
}))
vi.mock('@panwatch/biz-ui/hooks/useECharts', () => ({
  useECharts: () => ({ ref: () => {}, chartRef: { current: { setOption: vi.fn(), resize: vi.fn() } } }),
}))

import KpiBand, { useMainlineTop1, usePhaseLabel, type PhaseKpi } from '@panwatch/biz-ui/components/KpiBand'
import MarketMainlineCard from '@panwatch/biz-ui/components/MarketMainlineCard'
import MarketPhaseCard from '@panwatch/biz-ui/components/MarketPhaseCard'
import { PhaseGaugeCard } from '@/pages/Dashboard'

const NOTE = '尚未同步阶段数据, 请调用 POST /api/market/phase/sync'
const TIMEOUT = '请求超时，请稍后重试'

beforeEach(() => {
  api.fail = null
  api.resp = null
})
afterEach(() => cleanup())

const basePhase = (over: Partial<PhaseKpi>): PhaseKpi => ({
  label: null,
  loading: false,
  error: null,
  unavailableNote: null,
  limitUp: null,
  sealRate: null,
  reload: () => {},
  ...over,
})

function band(phase: PhaseKpi, mainlineError: string | null = null) {
  return render(
    <MemoryRouter>
      <KpiBand
        upCount={3200}
        downCount={1800}
        mainFlowYi={-120.5}
        amountYi={9800}
        phase={phase}
        mainlineTop1={null}
        mainlineLoading={false}
        mainlineError={mainlineError}
      />
    </MemoryRouter>,
  )
}

const MAINLINE_RESP = {
  total_groups: 12,
  ranked_groups: Array.from({ length: 10 }).map((_, i) => ({
    name: `题材${i}`,
    limit_up_count: 5 - (i % 3),
    ge2_count: 2,
    max_boards: 3,
    boards_sum: 9,
    rungs: 2,
    score: 80 - i,
    leader: { code: `60000${i}`, name: `龙头${i}`, days: 2, amount: 1e8 },
    constituents: [],
  })),
}

describe('KpiBand 情绪周期 / 涨停跌停 (走查①②)', () => {
  it('phase 拉取失败 → 情绪周期与涨停/跌停都显式"加载失败", 不塌成缺值', () => {
    band(basePhase({ error: TIMEOUT }))
    // 情绪周期 + 涨停/跌停 两格都是失败态
    expect(screen.getAllByText('加载失败').length).toBe(2)
    const cell = screen.getByText('情绪周期').closest('[title]')
    expect(cell?.getAttribute('title')).toContain(TIMEOUT)
  })

  it('phase 无数据(available:false) → 显缺值符号且悬停显示后端 note 原文', () => {
    band(basePhase({ unavailableNote: NOTE }))
    expect(screen.queryByText('加载失败')).toBeNull()
    expect(screen.getByText('情绪周期').closest('[title]')?.getAttribute('title')).toBe(NOTE)
  })

  it('跌停缺值一律按缺值符号渲染, 不再显示误导性的"暂无"', () => {
    band(basePhase({ label: '主升', limitUp: 61, sealRate: 0.62 }))
    expect(screen.queryByText('暂无')).toBeNull()
    expect(screen.getByText('61')).toBeTruthy()
    expect(screen.getByText('封板率 62%')).toBeTruthy()
  })

  it('主线 Top1 拉取失败 → 显式失败, 不塌成缺值', () => {
    band(basePhase({ label: '主升' }), '服务不可用')
    const cell = screen.getByText('主线 Top1').closest('[title]')
    expect(cell?.getAttribute('title')).toContain('服务不可用')
    expect(screen.getByText('加载失败')).toBeTruthy()
  })
})

describe('usePhaseLabel / useMainlineTop1 不得吞掉失败 (走查①②)', () => {
  it('usePhaseLabel: 失败写入 error, 不再静默', async () => {
    api.fail = TIMEOUT
    const { result } = renderHook(() => usePhaseLabel())
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.error).toBe(TIMEOUT)
    expect(result.current.label).toBeNull()
  })

  it('usePhaseLabel: available:false 透传后端 note 原文', async () => {
    api.resp = { available: false, current: null, note: NOTE }
    const { result } = renderHook(() => usePhaseLabel())
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.error).toBeNull()
    expect(result.current.unavailableNote).toBe(NOTE)
  })

  it('usePhaseLabel: 有数据时取 首板+≥2板 作为涨停数', async () => {
    api.resp = { available: true, current: { label: '主升', first_board: 40, ge2_count: 21, seal_rate: 0.62 }, note: '' }
    const { result } = renderHook(() => usePhaseLabel())
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.label).toBe('主升')
    expect(result.current.limitUp).toBe(61)
  })

  it('useMainlineTop1: 失败写入 error', async () => {
    api.fail = 'boom'
    const { result } = renderHook(() => useMainlineTop1())
    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.error).toBe('boom')
    expect(result.current.top).toBeNull()
  })
})

describe('市场温度卡 / 情绪周期阶段卡 (走查③④)', () => {
  it('市场温度: 拉取失败 → 显式失败 + 重试, 不再常驻"阶段数据同步中…"', async () => {
    api.fail = TIMEOUT
    render(<PhaseGaugeCard />)
    expect(await screen.findByText(`市场温度加载失败: ${TIMEOUT}`)).toBeTruthy()
    expect(screen.getByRole('button', { name: '重试' })).toBeTruthy()
    expect(screen.queryByText(/阶段数据同步中/)).toBeNull()
  })

  it('市场温度: 无数据 → 后端 note 原文, 且空态占满一个仪表盘高度', async () => {
    api.resp = { available: false, current: null, note: NOTE }
    const { container } = render(<PhaseGaugeCard />)
    expect(await screen.findByText(`市场温度暂不可用: ${NOTE}`)).toBeTruthy()
    expect(container.querySelector('[class*="h-[154px]"]')).toBeTruthy()
    expect(screen.queryByText(/阶段数据同步中/)).toBeNull()
  })

  it('市场温度: 有数据 → 渲染仪表盘', async () => {
    api.resp = { available: true, current: { phase: 'rally', label: '主升', max_height: 5, promo_rate: 0.3, seal_rate: 0.6 }, note: '' }
    render(<PhaseGaugeCard />)
    expect(await screen.findByText('市场温度 · 主升')).toBeTruthy()
  })

  it('情绪周期阶段卡: available:false 不再伪装成"积累中"阶段', async () => {
    api.resp = { available: false, current: null, recent_30d: [], distribution: [], total_days: 0, note: NOTE }
    render(<MarketPhaseCard />)
    expect(await screen.findByText(`暂无阶段数据: ${NOTE}`)).toBeTruthy()
    expect(screen.queryByText('积累中')).toBeNull()
  })
})

describe('市场全景网格行高 (走查④)', () => {
  it('主线 Top10 列表在 lg 起限高滚动, 不再把同一行顶出大块死白', async () => {
    api.resp = MAINLINE_RESP
    const { container } = render(
      <MemoryRouter>
        <MarketMainlineCard />
      </MemoryRouter>,
    )
    expect(await screen.findByText('题材0')).toBeTruthy()
    const list = container.querySelector('[class*="max-h-[300px]"]')
    expect(list).toBeTruthy()
    expect(list?.className).toContain('lg:overflow-y-auto')
    // 10 行仍在 DOM 里(只是可视区限高), 不丢数据
    expect(screen.getByText('题材9')).toBeTruthy()
  })
})
