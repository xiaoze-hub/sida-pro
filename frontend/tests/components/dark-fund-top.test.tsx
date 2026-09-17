// @vitest-environment jsdom
// 暗盘资金 TOP 榜页面 (2026-09-14 走查缺陷修复):
// ① .tck 暗盘对照列全空 → 整列隐藏(不留 20 行恒空列), 有数据时列头明示"仅持仓股";
// ② 金额单位: 榜单字段后端明确是**万元**(main_net_wan/total_amount_wan/tck_dark_net_wan),
//    旧代码用 toWan(= toAmount 元口径)再除 1e4 ⇒ 11.68亿 显示成 "11.68万"(小 1 万倍)。
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({ darkFundTop: vi.fn() }))

vi.mock('@panwatch/api', () => ({
  marketScanApi: {
    darkFundTop: (...a: unknown[]) => mocks.darkFundTop(...a),
    refreshDarkFundTop: vi.fn(),
  },
}))
vi.mock('react-router-dom', () => ({ useNavigate: () => vi.fn() }))

import DarkFundTopPage from '@/pages/DarkFundTop'

/** 一行榜单数据; tck 字段不传 = 非持仓股(后端不给该 key) */
function row(over: Record<string, unknown> = {}) {
  return {
    symbol: '600519',
    name: '贵州茅台',
    ths_code: 'USHA600519',
    main_net_wan: 88000,
    main_net_ratio: 1234,
    total_amount_wan: 116836.13,
    source: 'thsdk_dde',
    ...over,
  }
}

function snapshot(top: Array<Record<string, unknown>>) {
  return {
    available: true,
    snapshot_date: '2026-09-11',
    market: 'cn',
    updated_at: '2026-09-11 15:31:02',
    universe: 5141,
    computed: 5100,
    generated_at: '2026-09-11T15:31:02',
    top,
  }
}

/**
 * 表头列名列表(role=columnheader)。
 *
 * 2026-09-18: 页面同时渲染**桌面表格**与**移动端卡片列表**(`hidden md:block` / `md:hidden`),
 * jsdom 不套用 CSS ⇒ 同一行文字会命中两次(`getByText` 报 "Found multiple elements")。
 * 故本文件的断言一律 `within(桌面表格)` 收窄; 卡片的渲染由 `dark-fund-top` 走查基线另管。
 */
function headers(): string[] {
  return within(tbl()).getAllByRole('columnheader').map((th) => th.textContent ?? '')
}

/** 桌面表格(唯一带 role=table 的容器) */
function tbl(): HTMLElement {
  return screen.getByRole('table')
}

describe('DarkFundTop 暗盘资金 TOP 榜', () => {
  afterEach(() => cleanup())
  beforeEach(() => mocks.darkFundTop.mockReset())

  it('全部行都没有 .tck 数据 → 整列隐藏, 页脚说明列被隐藏(不留恒空列)', async () => {
    mocks.darkFundTop.mockResolvedValue(snapshot([row(), row({ symbol: '000001', name: '平安银行' })]))
    render(<DarkFundTopPage />)
    await screen.findAllByText('贵州茅台')

    expect(headers().some((h) => h.includes('tck 暗盘对照'))).toBe(false)
    expect(screen.getByText(/该列已隐藏/)).toBeTruthy()
    expect(screen.getByText(/都不是持仓股/)).toBeTruthy()
  })

  it('有持仓股数据 → 列出现且列头明示口径范围(信息不丢)', async () => {
    mocks.darkFundTop.mockResolvedValue(
      snapshot([row({ tck_dark_net_wan: 15000 }), row({ symbol: '000001', name: '平安银行' })]),
    )
    render(<DarkFundTopPage />)
    await screen.findAllByText('贵州茅台')

    const tckHeader = headers().find((h) => h.includes('tck 暗盘对照'))
    expect(tckHeader).toBeTruthy()
    expect(tckHeader).toContain('仅持仓股')
    // 持仓股那行按万元口径 → 1.50亿; 非持仓股那行显式 '--', 不编造
    expect(within(tbl()).getByText('+1.50亿')).toBeTruthy()
    expect(within(tbl()).getByText('--')).toBeTruthy()
  })

  it('金额按万元口径渲染: 11.68亿 不能被显示成 "11.68万"', async () => {
    mocks.darkFundTop.mockResolvedValue(snapshot([row()]))
    render(<DarkFundTopPage />)
    await screen.findAllByText('贵州茅台')

    // 主力净流入(有方向) → 带符号
    expect(within(tbl()).getByText('+8.80亿')).toBeTruthy()
    // 总成交额(规模量) → 不带 '+', 且量级是亿
    expect(within(tbl()).getByText('11.68亿')).toBeTruthy()
    expect(screen.queryByText('+11.68亿')).toBeNull()
    expect(screen.queryByText('8.80万')).toBeNull()
    expect(screen.queryByText('11.68万')).toBeNull()
  })

  it('总成交额缺失(上游 int32 哨兵/无数据) → 显式 --, 不塌成 0', async () => {
    mocks.darkFundTop.mockResolvedValue(snapshot([row({ total_amount_wan: null })]))
    render(<DarkFundTopPage />)
    await screen.findAllByText('贵州茅台')

    expect(within(tbl()).getByText('--')).toBeTruthy()
    expect(screen.queryByText('0.00万')).toBeNull()
  })

  it('主力净量是字符串脏数(PG DECIMAL)时不崩', async () => {
    mocks.darkFundTop.mockResolvedValue(
      snapshot([row({ main_net_ratio: '1234.0' as unknown as number, name: '脏数股' })]),
    )
    render(<DarkFundTopPage />)
    await screen.findAllByText('脏数股')

    expect(within(tbl()).getByText('1234')).toBeTruthy()
  })
})
