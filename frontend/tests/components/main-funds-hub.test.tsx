// @vitest-environment jsdom
//
// 主力资金工作区(MainFundsHub)钉子:
//   ① 并排入口: 默认落「暗盘资金 TOP」, 点 Tab 切「主力战报」且 URL 同步 ?tab=;
//   ② 深链: ?tab=war 直达战报(不先渲染暗盘), ?tab=war-report 等旧别名归一为 canonical key,
//      未知 tab 回退默认并改写 URL;
//   ③ 权限无损: 两页权限点不同(view_dark / view_forecast), 只有其中一个权限的用户必须仍能
//      进工作区并看到自己能看的那一页 —— 守卫取「任一」, Tab 各自过滤; 全无权限才显式拒绝;
//   ④ 口径差异: 页首必须写明两页数据源/时点/更新方式不同, 及万元 vs 亿元不可相加;
//   ⑤ 接线契约: 旧路径 → ?tab= 的重定向映射与 canonical key 对齐(主模型接线不能出现死 Tab)。
// 禁真实网络: 只 mock @sida/api; 两个页面组件走真实代码(既有功能原样保留)。
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  darkFundTop: vi.fn(),
  refreshDarkFundTop: vi.fn(),
  warDaily: vi.fn(),
  warRefresh: vi.fn(),
}))

vi.mock('@sida/api', () => ({
  marketScanApi: {
    darkFundTop: (...a: unknown[]) => mocks.darkFundTop(...a),
    refreshDarkFundTop: (...a: unknown[]) => mocks.refreshDarkFundTop(...a),
  },
  warReportApi: {
    daily: (...a: unknown[]) => mocks.warDaily(...a),
    refresh: (...a: unknown[]) => mocks.warRefresh(...a),
  },
}))

import MainFundsHubPage, {
  LEGACY_MAIN_FUNDS_ROUTES,
  MAIN_FUNDS_HUB_PATH,
  MAIN_FUNDS_HUB_PERMS,
  MAIN_FUNDS_HUB_TABS,
  canEnterMainFundsHub,
  legacyMainFundsRedirect,
} from '@/pages/MainFundsHub'

/** URL 探针: 断言 Tab 状态真的落进了 ?tab=(可分享/可刷新/可后退) */
function LocProbe() {
  const loc = useLocation()
  return <div data-testid="loc">{`${loc.pathname}${loc.search}`}</div>
}

function darkSnapshot() {
  return {
    available: true,
    snapshot_date: '2026-11-06',
    market: 'cn',
    updated_at: '2026-11-06 15:31:02',
    universe: 5141,
    computed: 5100,
    generated_at: '2026-11-06T15:31:02',
    top: [
      {
        symbol: '600519',
        name: '贵州茅台',
        ths_code: 'USHA600519',
        main_net_wan: 88000,
        main_net_vol: 1234,
        total_amount_wan: 116836.13,
        source: 'thsdk_dde',
      },
    ],
  }
}

function warSnapshot() {
  return {
    available: true,
    snapshot_date: '2026-11-06',
    market: 'CN',
    updated_at: '2026-11-06T15:31:02',
    generated_at: '2026-11-06T15:31:02',
    universe: 5141,
    computed: 5100,
    top_inflow: [
      { symbol: '600519', name: '贵州茅台', main_net_wan: 88000, source: 'thsdk_dde', caliber: 'ths' },
    ],
    top_outflow: [
      { symbol: '000001', name: '平安银行', main_net_wan: -50000, source: 'thsdk_dde', caliber: 'ths' },
    ],
    industry: { available: false, note: '行业资金(SUPAMO)源不可用', rows: [] },
    split_wash: {
      available: false,
      split_count: null,
      wash_count: null,
      covered: [],
      note: '无委托号级 .tck 数据源',
    },
  }
}

const BOTH = new Set(['view_dark', 'view_forecast'])

function renderHub(entry = '/funds', myPerms: Set<string> | null = BOTH) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <MainFundsHubPage myPerms={myPerms} />
      <LocProbe />
    </MemoryRouter>,
  )
}

const loc = () => screen.getByTestId('loc').textContent ?? ''
const tabNames = () => screen.getAllByRole('tab').map((t) => t.textContent ?? '')
const tab = (name: RegExp) => screen.getByRole('tab', { name })

describe('MainFundsHub 主力资金工作区', () => {
  afterEach(() => cleanup())
  beforeEach(() => {
    for (const m of Object.values(mocks)) m.mockReset()
    mocks.darkFundTop.mockResolvedValue(darkSnapshot())
    mocks.warDaily.mockResolvedValue(warSnapshot())
  })

  it('默认落「暗盘资金 TOP」, 且不往 URL 里写冗余 tab(与既有页面直达行为一致)', async () => {
    renderHub()
    await screen.findAllByText('贵州茅台')

    expect(tabNames()).toEqual(['暗盘资金 TOP', '主力战报'])
    expect(loc()).toBe('/funds')
    expect(mocks.darkFundTop).toHaveBeenCalledTimes(1)
    // 战报未挂载就不该取数(不做双页预取)
    expect(mocks.warDaily).not.toHaveBeenCalled()
  })

  it('点「主力战报」→ 换页 + URL 写 ?tab=war; 战报页原有内容(testid/空相子块)照旧', async () => {
    renderHub()
    await screen.findAllByText('贵州茅台')

    fireEvent.click(tab(/主力战报/))

    await waitFor(() => expect(loc()).toBe('/funds?tab=war'))
    expect(await screen.findByTestId('war-report')).toBeTruthy()
    expect(mocks.warDaily).toHaveBeenCalledTimes(1)
    // 战报缺源子块仍显式「无数据」(并排后不许塌成 0)
    expect(await screen.findByText(/行业资金\(SUPAMO\)暂无数据/)).toBeTruthy()
    // 切走后暗盘页卸载, 再切回要能重新取数(各页刷新/取数逻辑不变)
    fireEvent.click(tab(/暗盘资金 TOP/))
    await waitFor(() => expect(loc()).toBe('/funds?tab=dark'))
    await screen.findAllByText('贵州茅台')
    expect(mocks.darkFundTop).toHaveBeenCalledTimes(2)
  })

  it('深链 ?tab=war 直达战报: 不先挂暗盘页(不浪费一次全市场请求)', async () => {
    renderHub('/funds?tab=war')
    expect(await screen.findByTestId('war-report')).toBeTruthy()
    expect(mocks.darkFundTop).not.toHaveBeenCalled()
    expect(loc()).toBe('/funds?tab=war')
  })

  it('旧别名 ?tab=war-report / ?tab=dark-fund-top 归一为 canonical key', async () => {
    renderHub('/funds?tab=war-report')
    await waitFor(() => expect(loc()).toBe('/funds?tab=war'))
    expect(await screen.findByTestId('war-report')).toBeTruthy()
  })

  it('别名 ?tab=dark-fund-top → ?tab=dark(replace, 不留历史垃圾)', async () => {
    renderHub('/funds?tab=dark-fund-top&x=1')
    await waitFor(() => expect(loc()).toBe('/funds?tab=dark&x=1'))
    await screen.findAllByText('贵州茅台')
    expect(mocks.warDaily).not.toHaveBeenCalled()
  })

  it('未知 ?tab= → 回退默认 Tab 并把 URL 归一(不留非法值)', async () => {
    renderHub('/funds?tab=nope')
    await waitFor(() => expect(loc()).toBe('/funds?tab=dark'))
    await screen.findAllByText('贵州茅台')
  })

  // ── 权限无损(并排入口唯一真实回归风险) ──────────────────────────────────
  it('只有 view_forecast 的用户: 仍能进工作区, 只剩战报 Tab, 默认即战报', async () => {
    expect(canEnterMainFundsHub(new Set(['view_forecast']))).toBe(true)
    renderHub('/funds', new Set(['view_forecast']))

    expect(await screen.findByTestId('war-report')).toBeTruthy()
    expect(tabNames()).toEqual(['主力战报'])
    // 无 tab 参数时不写 URL(直接就是唯一可见页)
    expect(loc()).toBe('/funds')
    expect(mocks.darkFundTop).not.toHaveBeenCalled()
  })

  it('只有 view_dark 的用户: 深链请求战报 → 归一到暗盘, 不会看到无权内容', async () => {
    renderHub('/funds?tab=war', new Set(['view_dark']))
    await waitFor(() => expect(loc()).toBe('/funds?tab=dark'))
    await screen.findAllByText('贵州茅台')
    expect(screen.queryByTestId('war-report')).toBeNull()
    expect(mocks.warDaily).not.toHaveBeenCalled()
  })

  it('两个权限都没有 → 显式「无可用页签(权限不足)」, 不取数、不空白', async () => {
    expect(canEnterMainFundsHub(new Set())).toBe(false)
    renderHub('/funds', new Set())

    expect(screen.getByText(/无可用页签\(权限不足\)/)).toBeTruthy()
    expect(mocks.darkFundTop).not.toHaveBeenCalled()
    expect(mocks.warDaily).not.toHaveBeenCalled()
  })

  it('权限未加载(null)不预判拒绝: 两个 Tab 先都在, 交给页面内过滤', () => {
    expect(canEnterMainFundsHub(null)).toBe(true)
    renderHub('/funds', null)
    expect(tabNames()).toEqual(['暗盘资金 TOP', '主力战报'])
  })

  // ── 口径差异必须上屏 ────────────────────────────────────────────────────
  it('页首口径条: 写明两页数据源/时点/更新方式, 且点明不可跨页相加', async () => {
    renderHub()
    const strip = screen.getByTestId('main-funds-caliber')
    const text = strip.textContent ?? ''

    expect(text).toContain('thsdk_dde')
    expect(text).toContain('TQ SUPAMO')
    // 时点各自独立(不许暗示同一快照)
    expect(text).toContain('不保证同日同刻')
    expect(text).toContain('万元')
    expect(text).toContain('亿元')
    expect(text).toContain('不可跨页相加')
    // 两页各占一行, 刷新方式也各自独立
    expect(text).toContain('重新扫描')
    expect(text).toContain('重新生成')
    expect(strip.querySelectorAll('[data-fund-row]').length).toBe(2)
  })

  // ── 接线契约(主模型在 App.tsx 侧执行) ──────────────────────────────────
  it('旧路径重定向映射指向 canonical tab, 且带 ?tab=(否则返回键会丢上下文)', () => {
    const canonical = Object.values(MAIN_FUNDS_HUB_TABS) as string[]
    for (const key of Object.values(LEGACY_MAIN_FUNDS_ROUTES)) {
      expect(canonical).toContain(key)
    }
    expect(legacyMainFundsRedirect('/dark-fund-top')).toBe(`${MAIN_FUNDS_HUB_PATH}?tab=${MAIN_FUNDS_HUB_TABS.dark}`)
    expect(legacyMainFundsRedirect('/war-report')).toBe(`${MAIN_FUNDS_HUB_PATH}?tab=${MAIN_FUNDS_HUB_TABS.war}`)
    // 非旧路径不误跳
    expect(legacyMainFundsRedirect('/opportunities')).toBeNull()
    expect(legacyMainFundsRedirect('/funds')).toBeNull()
  })

  it('守卫权限清单 = 两页各自权限点(任一放行), 不是单点', () => {
    expect([...MAIN_FUNDS_HUB_PERMS].sort()).toEqual(['view_dark', 'view_forecast'])
    for (const perm of MAIN_FUNDS_HUB_PERMS) {
      expect(canEnterMainFundsHub(new Set([perm]))).toBe(true)
    }
    expect(canEnterMainFundsHub(new Set(['view_quotes']))).toBe(false)
  })
})
