// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const fetchAPI = vi.fn()
vi.mock('@panwatch/api', () => ({ fetchAPI: (...a: unknown[]) => fetchAPI(...a) }))

import MarketBroadcast, { computeBroadcast } from '@/components/dashboard/MarketBroadcast'

afterEach(() => cleanup())
beforeEach(() => fetchAPI.mockClear())

const FULL = {
  available: true,
  trade_date: '20261023',
  quality: 'ok',
  phase_label: '修复',
  limit_up_count: 82,
  first_board_count: 70,
  ge2_count: 12,
  ge3_count: 3,
  limit_up_max_streak: 6,
  one_word_count: 2,
  broken_count: 12,
  break_rate_pct: 12.77,
  seal_rate_pct: 87.23,
  promo_rate_pct: 36.36,
  limit_down_count: 6,
  top_sectors: [
    { name: '专用设备', count: 8 },
    { name: '软件开发', count: 7 },
    { name: 'IT服务Ⅱ', count: 6 },
    { name: '汽车零部', count: 6 },
    { name: '通用设备', count: 5 },
    { name: '自动化设', count: 5 },
    { name: '第七个', count: 4 },
  ],
  leaders: [
    { sector: '专用设备', symbol: '002535', name: '林州重机', board_days: 2, first_seal: '09:30:00' },
    { sector: '软件开发', symbol: '603528', name: '多伦科技', board_days: 2, first_seal: '13:19:25' },
  ],
  sector_caliber: '涨停家数按行业聚合（非板块涨跌幅榜）',
}

describe('computeBroadcast（纯函数）', () => {
  it('梯队与质量拆成两句，数字都来自接口', () => {
    const f = computeBroadcast(FULL)
    // 梯队不再重复「涨停 82」(上方 KPI 已有)，只补结构
    expect(f.ladder).toContain('首板 70')
    expect(f.ladder).toContain('2板 12')
    expect(f.ladder).toContain('3板+ 3')
    expect(f.ladder).toContain('最高 6 板')
    expect(f.ladder).not.toContain('涨停 82')
    // 质量格：比率为主，跌停放这里
    expect(f.quality).toContain('封板 87.2%')
    expect(f.quality).toContain('炸板 12')
    expect(f.quality).toContain('炸板率 12.8%')
    expect(f.quality).toContain('晋级 36.4%')
    expect(f.quality).toContain('跌停 6')
  })

  it('板块最多 4 个（横向窄格，多了会换行撑高卡片）', () => {
    const f = computeBroadcast(FULL)
    expect(f.sectors.split(' · ').length).toBe(4)
    expect(f.sectors).toContain('专用设备 8')
    expect(f.sectors).not.toContain('自动化设')
    expect(f.sectors).not.toContain('第七个')
  })

  it('龙头带连板数与封板时间', () => {
    const f = computeBroadcast(FULL)
    expect(f.leaders).toContain('林州重机 2板 09:30')
    expect(f.leaders).toContain('多伦科技 2板 13:19')
  })

  it('注：首板龙头(1板)不显示"1板"噪音', () => {
    const f = computeBroadcast({
      ...FULL,
      leaders: [{ name: '榕基软件', board_days: 1, first_seal: '09:31:57' }],
    })
    expect(f.leaders).not.toContain('1板')
    expect(f.leaders).toContain('榕基软件')
  })

  it('空/不可用时全部返回空串，不编造', () => {
    expect(computeBroadcast(null).ladder).toBe('')
    expect(computeBroadcast({ available: false }).sectors).toBe('')
  })

  it('缺字段不造 0：没有 promo_rate_pct 就不出现"晋级"', () => {
    const f = computeBroadcast({ available: true, limit_up_count: 50 })
    expect(f.quality).not.toContain('晋级')
    expect(f.ladder).toBe('')  // 只有涨停家数时梯队格为空，不编结构
  })
})

describe('MarketBroadcast 四态', () => {
  it('有数据：渲染梯队/板块/龙头/口径', async () => {
    fetchAPI.mockResolvedValueOnce(FULL)
    render(<MarketBroadcast />)
    await waitFor(() => expect(screen.getByTestId('broadcast')).toBeTruthy())
    expect(screen.getByTestId('broadcast-ladder').textContent).toContain('首板 70')
    expect(screen.getByTestId('broadcast-sectors').textContent).toContain('专用设备 8')
    // 口径并入「涨停潮」标题，不再单独占一行（省卡片高度）
    expect(screen.getByTestId('broadcast-caliber').textContent).toContain('非涨幅榜')
    expect(screen.getByTestId('broadcast').textContent).toContain('基准日 2026-10-23')
  })

  it('无数据：显式说"尚未生成"，不显示 0', async () => {
    fetchAPI.mockResolvedValueOnce({ available: false, reason: '暂无盘后复盘数据（当日尚未生成）' })
    render(<MarketBroadcast />)
    await waitFor(() => expect(screen.getByTestId('broadcast-empty')).toBeTruthy())
    expect(screen.getByTestId('broadcast-empty').textContent).toContain('尚未生成')
    expect(document.body.textContent).not.toContain('涨停 0')
  })

  it('读取失败：显式报错 + 重试提示，不伪装空态', async () => {
    fetchAPI.mockRejectedValueOnce(new Error('502'))
    render(<MarketBroadcast />)
    await waitFor(() => expect(screen.getByTestId('broadcast-error')).toBeTruthy())
    expect(screen.getByTestId('broadcast-error').textContent).toContain('读取失败')
  })

  it('quality 非 ok 时标出来（数据不全不能装作满的）', async () => {
    fetchAPI.mockResolvedValueOnce({ ...FULL, quality: 'partial' })
    render(<MarketBroadcast />)
    await waitFor(() => expect(screen.getByTestId('broadcast')).toBeTruthy())
    expect(screen.getByTestId('broadcast').textContent).toContain('数据不全')
  })

  it('缺 top_sectors 字段不白屏', async () => {
    fetchAPI.mockResolvedValueOnce({ available: true, limit_up_count: 30, first_board_count: 20 })
    render(<MarketBroadcast />)
    await waitFor(() => expect(screen.getByTestId('broadcast')).toBeTruthy())
    expect(screen.queryByTestId('broadcast-sectors')).toBeNull()
    expect(screen.getByTestId('broadcast-ladder').textContent).toContain('首板 20')
  })

  it('横向四格：容器是 grid，不是竖排堆叠', async () => {
    fetchAPI.mockResolvedValueOnce(FULL)
    render(<MarketBroadcast />)
    await waitFor(() => expect(screen.getByTestId('broadcast')).toBeTruthy())
    const grid = screen.getByTestId('broadcast-ladder').parentElement?.parentElement
    expect(grid?.className).toContain('grid')
    expect(grid?.className).toContain('grid-cols-4')
  })

  it('每个格子标题都是 semibold（用户要求标题形态突出）', async () => {
    fetchAPI.mockResolvedValueOnce(FULL)
    render(<MarketBroadcast />)
    await waitFor(() => expect(screen.getByTestId('broadcast')).toBeTruthy())
    const titles = ['涨停梯队', '封板质量', '涨停潮', '龙头']
    for (const t of titles) {
      const el = screen.getByText(t)
      expect(el.className).toContain('font-semibold')
    }
  })
})
