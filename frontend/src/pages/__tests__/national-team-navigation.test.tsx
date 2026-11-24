// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useNavigate } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import NationalTeam from '../NationalTeam'

const { overview, holdings, changes } = vi.hoisted(() => ({
  overview: vi.fn(), holdings: vi.fn(), changes: vi.fn(),
}))
vi.mock('@sida/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@sida/api')>()),
  nationalTeamApi: { overview, holdings, changes },
}))

const current = '2026-09-30'
const yoy = '2025-09-30'
const qoq = '2026-06-30'
const base = `/national-team?report_date=${current}&holder=huijin&view=changes`

function OpenYoYLink() {
  const navigate = useNavigate()
  return <button type="button" onClick={() => navigate(`${base}&prev_report_date=${yoy}&q=601398`)}>打开同比链接</button>
}

beforeEach(() => {
  vi.clearAllMocks()
  overview.mockResolvedValue({ available: true, report_date: current, prev_report_date: qoq, groups: [] })
  holdings.mockResolvedValue({ items: [] })
  changes.mockImplementation(async ({ prev_report_date }: { prev_report_date?: string }) => ({
    available: true,
    report_date: current,
    prev_report_date: prev_report_date ?? qoq,
    comparison: prev_report_date ? 'yoy' : 'qoq',
    counts: { new: prev_report_date ? 9 : 2, increase: 0, decrease: 0, exit: 0, unchanged: 0 },
    items: [],
  }))
})
afterEach(cleanup)

describe('国家队比较期深链', () => {
  it('已打开环比页时，站内再次打开同比深链会重新取同比结果', async () => {
    render(<MemoryRouter initialEntries={[base]}><OpenYoYLink /><NationalTeam /></MemoryRouter>)
    await screen.findByText('新进 2')
    fireEvent.click(screen.getByRole('button', { name: '打开同比链接' }))
    await waitFor(() => expect(changes).toHaveBeenLastCalledWith(expect.objectContaining({ report_date: current, prev_report_date: yoy, holder: 'huijin', q: '601398' })))
    await screen.findByText('新进 9')
    expect(screen.getByRole('heading', { name: '同比变动（与去年同季）' })).toBeTruthy()
  })

  it('显式空比较期不发环比请求，除非用户选择改看环比', async () => {
    render(<MemoryRouter initialEntries={[`${base}&prev_report_date=`]}><NationalTeam /></MemoryRouter>)
    const resume = await screen.findByRole('button', { name: '改看环比' })
    expect(changes).not.toHaveBeenCalled()
    fireEvent.click(resume)
    await screen.findByText('新进 2')
    expect(changes).toHaveBeenLastCalledWith(expect.objectContaining({ prev_report_date: undefined }))
  })

  it('后端拒绝比较期时不展示 URL 指定的同比基准', async () => {
    changes.mockResolvedValueOnce({ available: false, status: 'prev_unavailable', items: [], reason: '未采集此比较期' })
    render(<MemoryRouter initialEntries={[`/national-team?view=changes&prev_report_date=${yoy}`]}><NationalTeam /></MemoryRouter>)
    await screen.findByText('未采集此比较期')
    expect(screen.queryByText(`同比基准 ${yoy}`)).toBeNull()
    expect(screen.getByRole('heading', { name: '比较期不可用' })).toBeTruthy()
  })
})
