// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * QuickRail 右栏速览卡(工作台 v2 三合一, spec §4.2 / 布局图 §1.2 右栏 320px)。
 *
 * 守四件事(都是本任务的绑定条款):
 *  ① 顺序 = 数智决策 → 盘口速览 → 基本面/股本 → 题材/板块; 根节点**不设宽**(320px 由页面给);
 *  ② 盘口速览真值落位(封单/主力净额 + 五档)且 **30s 轮询 + 失败保留旧值**;
 *     **去重**: 现价/涨停价 归带1 HeaderBand, 本卡不得渲染(即使接口给了真值);
 *  ③ 缺值一律 `--`(真数据纪律: 不编、不渲染 NaN/0);
 *  ④ CN-only 闸门(非 CN 标的绝不发通达信接口)。
 */
const mocks = vi.hoisted(() => ({ fetchAPI: vi.fn() }))

vi.mock('@panwatch/api', () => ({
  fetchAPI: (...args: unknown[]) => mocks.fetchAPI(...args),
}))

// 数智决策卡本身由 Task 4 的单测守, 这里只守"它是本栏第一张卡、只出现一次"。
vi.mock('@panwatch/biz-ui/components/workbench/DecisionCard', () => ({
  default: (p: { symbol: string; market: string }) => (
    <div data-testid="decision">{`decision:${p.symbol}:${p.market}`}</div>
  ),
}))

import QuickRail from '@panwatch/biz-ui/components/workbench/QuickRail'

const L2_POLL_MS = 30000

const L2_FULL = {
  symbol: '002636',
  as_of: '2026-09-13T14:30:12+08:00',
  note: null,
  snapshot: {
    now: 10.5,
    amount: 123456789,
    buyp: [10.49, 10.48, 10.47, 10.46, 10.45],
    buyv: [1200, 2300, 3400, 4500, 5600],
    sellp: [10.51, 10.52, 10.53, 10.54, 10.55],
    sellv: [1100, 2200, 3300, 4400, 5500],
  },
  // FCAmo 后端已从万元换算成元; Zjl_HB 为负 = 净流出(旧卡会渲染成裸 -18000000)
  more: { zt_price: 11.55, fcamo: 23000000, zjl_hb: -18000000, pe_ttm: 28.56, pb: 3.21 },
}

const FUND_FULL = {
  gb: { date: '20260912', ltgb: 123456789, zgb: 9876543210 },
  listing: { name: '某股', listing_date: '20200101' },
  sub_new: true,
  note: null,
}

const BLOCKS_FULL = {
  blocks: [
    { code: '880001', name: '半导体', type: '行业' },
    { code: '880002', name: '存储芯片', type: '题材' },
  ],
  note: null,
}

/** 按 URL 分流返回真值; 未预期 URL 直接抛(测试失败要响)。 */
function respond(url: unknown) {
  const u = String(url)
  if (u.includes('/l2')) return L2_FULL
  if (u.includes('/fundamental')) return FUND_FULL
  if (u.includes('/blocks')) return BLOCKS_FULL
  throw new Error(`unexpected url ${u}`)
}

/** 取某一行(左标签右数值)的数值文本。 */
function rowValue(container: HTMLElement, label: string): string | null {
  const row = Array.from(container.querySelectorAll('div')).find(
    (d) => d.children.length === 2 && d.children[0].textContent === label,
  )
  return row ? row.children[1].textContent : null
}

beforeEach(() => {
  mocks.fetchAPI.mockReset()
  mocks.fetchAPI.mockImplementation(async (url: unknown) => respond(url))
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('QuickRail 右栏速览', () => {
  it('四卡顺序 + 根节点不设宽(320px 由页面外壳给)', async () => {
    const { container } = render(<QuickRail symbol="002636" market="CN" />)
    const root = container.firstElementChild as HTMLElement
    expect(root.className).toBe('flex flex-col gap-2')
    expect(root.className).not.toContain('w-[')

    await waitFor(() => expect(screen.getByText('盘口速览')).toBeTruthy())
    const txt = container.textContent ?? ''
    const order = [
      txt.indexOf('decision:002636:CN'),
      txt.indexOf('盘口速览'),
      txt.indexOf('基本面 / 股本'),
      txt.indexOf('题材 / 板块'),
    ]
    expect(order.every((i) => i >= 0)).toBe(true)
    expect(order).toEqual([...order].sort((a, b) => a - b))
    // 数智决策只出现一次(去重: 本卡不重复任何三指标/共振读数)
    expect(screen.getAllByTestId('decision')).toHaveLength(1)
  })

  it('盘口速览真值落位: 两行(封单/主力净额) + 五档买卖价量 + 负值金额带单位', async () => {
    const { container } = render(<QuickRail symbol="002636" market="CN" />)
    await waitFor(() => expect(rowValue(container, '封单')).toBe('2300万'))
    expect(rowValue(container, '主力净额')).toBe('-1800万')
    // 快照时间戳(取数时刻, 守 stale 透明)
    expect(screen.getByText('快照 14:30:12')).toBeTruthy()

    const txt = container.textContent ?? ''
    for (const v of ['10.49', '10.45', '10.51', '10.55', '1200', '5600', '1100', '5500']) {
      expect(txt).toContain(v)
    }
  })

  it('去重(裁定): 现价/涨停价 归带1 HeaderBand, 本卡即使拿到真值也不渲染', async () => {
    // fixture 的 `snapshot.now = 10.5` / `more.zt_price = 11.55` 是真值 —— 接口给了也不许画,
    // 这正是去重契约(同一数据点全工作台只出现一处)的回归点: 重新加回任一行 → 本例如下断言失败。
    const { container } = render(<QuickRail symbol="002636" market="CN" />)
    await waitFor(() => expect(rowValue(container, '封单')).toBe('2300万'))
    expect(rowValue(container, '现价')).toBeNull()
    expect(rowValue(container, '涨停价')).toBeNull()
    expect(container.textContent).not.toContain('现价')
    expect(container.textContent).not.toContain('涨停价')
  })

  it('基本面/股本: PE(TTM)/PB 走 /l2 more, 股本=流通/总, 次新标注', async () => {
    const { container } = render(<QuickRail symbol="002636" market="CN" />)
    await waitFor(() => expect(rowValue(container, 'PE(TTM)')).toBe('28.56'))
    expect(rowValue(container, 'PB')).toBe('3.21')
    // 123456789 股 → 1.23亿; 9876543210 股 → 98.77亿
    expect(rowValue(container, '股本(流通/总)')).toBe('1.23亿 / 98.77亿')
    expect(screen.getByText('次新')).toBeTruthy()
  })

  it('题材/板块 chips 渲染 name + type', async () => {
    const { container } = render(<QuickRail symbol="002636" market="CN" />)
    await waitFor(() => expect(screen.getByText('半导体')).toBeTruthy())
    expect(screen.getByText('存储芯片')).toBeTruthy()
    expect(container.textContent).toContain('行业')
    expect(container.textContent).toContain('题材')
  })

  it('缺值一律 --(三卡都不编)', async () => {
    mocks.fetchAPI.mockImplementation(async (url: unknown) => {
      const u = String(url)
      if (u.includes('/l2')) return { symbol: '002636', as_of: null, note: null, snapshot: {}, more: {} }
      if (u.includes('/fundamental')) return { gb: null, listing: null, sub_new: null, note: null }
      if (u.includes('/blocks')) return { blocks: [], note: null }
      throw new Error(`unexpected url ${u}`)
    })
    const { container } = render(<QuickRail symbol="002636" market="CN" />)
    await waitFor(() => expect(rowValue(container, '封单')).toBe('--'))
    expect(rowValue(container, '主力净额')).toBe('--')
    // 去重: 现价/涨停价 两行本卡不存在(缺值时也不会以 `--` 形式出现)
    expect(rowValue(container, '现价')).toBeNull()
    expect(rowValue(container, '涨停价')).toBeNull()
    expect(rowValue(container, 'PE(TTM)')).toBe('--')
    expect(rowValue(container, 'PB')).toBe('--')
    expect(rowValue(container, '股本(流通/总)')).toBe('-- / --')
    expect(screen.queryByText(/快照/)).toBeNull()
    expect(screen.queryByText('次新')).toBeNull()
    // 空板块: 卡内只有标题 + '--' 占位(不画空 chips)
    const blocksCard = screen.getByText('题材 / 板块').parentElement as HTMLElement
    expect(blocksCard.textContent).toBe('题材 / 板块--')
  })

  it('30s 轮询 + 失败保留旧值(stale-on-error) + 恢复后上屏新值', async () => {
    let poll: (() => void) | null = null
    const realSetInterval = window.setInterval.bind(window)
    const intervalSpy = vi.spyOn(window, 'setInterval').mockImplementation(((
      fn: TimerHandler,
      ms?: number,
    ) => {
      if (ms === L2_POLL_MS) {
        poll = typeof fn === 'function' ? (fn as () => void) : null
        return 4242
      }
      return realSetInterval(fn as never, ms as never) as unknown as number
    }) as typeof window.setInterval)

    const { container } = render(<QuickRail symbol="002636" market="CN" />)
    await waitFor(() => expect(rowValue(container, '封单')).toBe('2300万'))
    expect(intervalSpy).toHaveBeenCalledWith(expect.any(Function), L2_POLL_MS)
    expect(poll).not.toBeNull()

    // 轮询失败 → 保留旧值, 不清零不编造
    mocks.fetchAPI.mockImplementation(async () => {
      throw new Error('boom')
    })
    await act(async () => {
      poll?.()
    })
    expect(rowValue(container, '封单')).toBe('2300万')

    // 恢复 → 新值上屏(观察点用封单: 现价已按去重裁定移出本卡)
    mocks.fetchAPI.mockImplementation(async (url: unknown) => {
      const u = String(url)
      if (u.includes('/l2')) return { ...L2_FULL, more: { ...L2_FULL.more, fcamo: 26000000 } }
      return respond(url)
    })
    await act(async () => {
      poll?.()
    })
    await waitFor(() => expect(rowValue(container, '封单')).toBe('2600万'))
  })

  it('换股先清旧值(不把上一只票的盘口画到新标的上)', async () => {
    const { container, rerender } = render(<QuickRail symbol="002636" market="CN" />)
    await waitFor(() => expect(rowValue(container, '封单')).toBe('2300万'))

    mocks.fetchAPI.mockImplementation(async () => {
      throw new Error('boom')
    })
    rerender(<QuickRail symbol="600519" market="CN" />)
    await waitFor(() => expect(rowValue(container, '封单')).toBe('--'))
    expect(rowValue(container, 'PE(TTM)')).toBe('--')
  })

  it('CN-only 闸门: 非 CN 标的不发通达信接口, 全部 --', async () => {
    const { container } = render(<QuickRail symbol="AAPL" market="US" />)
    await waitFor(() => expect(rowValue(container, '封单')).toBe('--'))
    expect(mocks.fetchAPI).not.toHaveBeenCalled()
    expect(rowValue(container, '股本(流通/总)')).toBe('-- / --')
    expect(screen.queryByText('半导体')).toBeNull()
  })

  // 下面的 fixture 是 2026-09-13 从本地后端直连真源探到的 002636 形态(非交易时段)。
  it('通达信真形态: 未封板(封单 0)/单档盘口/小额负净额/亿级股本', async () => {
    mocks.fetchAPI.mockImplementation(async (url: unknown) => {
      const u = String(url)
      if (u.includes('/l2'))
        return {
          symbol: '002636',
          as_of: '2026-09-13T12:22:18+08:00',
          note: null,
          snapshot: { now: 82.46, buyp: [82.45, 0, 0, 0, 0], buyv: [34, 0, 0, 0, 0], sellp: [], sellv: [] },
          more: { zt_price: 84.1, fcamo: 0, zjl_hb: -29575.36, pe_ttm: 60.26, pb: 14 },
        }
      if (u.includes('/fundamental'))
        return {
          gb: { date: '20260913', ltgb: 725234944, zgb: 728000000 },
          listing: { name: '某股', listing_date: '20111125' },
          sub_new: false,
          note: null,
        }
      return { blocks: [], note: null }
    })
    const { container } = render(<QuickRail symbol="002636" market="CN" />)
    await waitFor(() => expect(rowValue(container, '封单')).toBe('0'))
    // 未封板 → FCAmo = 0(真值, 不是缺值): 显示 '0' 而非 '--'
    // 去重: 真源同时给了 now=82.46 / zt_price=84.1, 本卡也不渲染这两行(归带1)
    expect(rowValue(container, '现价')).toBeNull()
    expect(rowValue(container, '涨停价')).toBeNull()
    // 小额净流出: 旧卡会渲染裸 -29575.36, 这里按万档带符号
    expect(rowValue(container, '主力净额')).toBe('-3万')
    expect(rowValue(container, 'PE(TTM)')).toBe('60.26')
    expect(rowValue(container, '股本(流通/总)')).toBe('7.25亿 / 7.28亿')
    const txt = container.textContent ?? ''
    expect(txt).toContain('82.45')
    expect(txt).toContain('34')
    expect(screen.queryByText('次新')).toBeNull()
  })

  it('板块反查 code 重复(实测多条为 "0")不产生 React 重复 key 警告', async () => {
    mocks.fetchAPI.mockImplementation(async (url: unknown) => {
      const u = String(url)
      if (u.includes('/l2')) return L2_FULL
      if (u.includes('/fundamental')) return FUND_FULL
      return {
        blocks: [
          { code: '881333.SH', name: '半导体', type: '行业' },
          { code: '0', name: '融资融券', type: '概念' },
          { code: '0', name: '深股通标的', type: '概念' },
          { code: '0', name: '指数成份', type: '指数' },
        ],
        note: null,
      }
    })
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<QuickRail symbol="002636" market="CN" />)
    await waitFor(() => expect(screen.getByText('指数成份')).toBeTruthy())
    expect(screen.getByText('融资融券')).toBeTruthy()
    const keyWarn = errSpy.mock.calls.filter((c) => String(c[0]).includes('same key'))
    expect(keyWarn).toHaveLength(0)
  })
})
