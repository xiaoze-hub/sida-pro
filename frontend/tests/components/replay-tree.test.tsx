// @vitest-environment jsdom
//
// 连板晋级树页(创新 1, 2026-10-24) —— 规格 specs/harness/replay_tree.md REPTREE-001..006。
//
// 钉六件事:
//  ① 梯队列 首板→高板 升序渲染, 家数与成员明细齐全; 明细截断显式标注(REPTREE-004);
//  ② 晋级率三件套必显: 样本 n + 同池基准(表内未存→口径缺口) + 未剔除一字板(REPTREE-002);
//  ③ 双口径同屏各自带标签: 东财三池 eastmoney_zt vs TQ 全A自算 tq_pool + 差值(REPTREE-003);
//  ④ payload 缺失 → 梯队显式「无数据」, 不用标量反推; 封单额/流通市值比显式无数据(REPTREE-004);
//  ⑤ 序列缺落库日渲染「未落库」+ '--', 绝不补 0(REPTREE-001); 点行切换树日期;
//  ⑥ 无快照 → available=false + 最近一期提示 + 查看按钮(REPTREE-006)。
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'

import ReplayTreePage from '@/pages/ReplayTree'
import ReplayPromotionTree from '@/components/ReplayPromotionTree'

const { treeMock, seriesMock } = vi.hoisted(() => ({ treeMock: vi.fn(), seriesMock: vi.fn() }))

vi.mock('@panwatch/api', () => ({
  marketReplayApi: { tree: treeMock, series: seriesMock },
}))

const TREE = {
  available: true,
  trade_date: '20261023',
  trade_date_iso: '2026-10-23',
  market: 'CN',
  source: 'eastmoney_replay',
  caliber: 'eastmoney_zt',
  quality: 'ok',
  missing_dims: [],
  limit_up_count: 82,
  broken_count: 12,
  break_rate_pct: 12.77,
  seal_rate_pct: 87.23,
  limit_down_count: 2,
  limit_down_max_days: 2,
  max_streak: 6,
  first_board_count: 78,
  ge2_count: 3,
  ge3_count: 3,
  ladder_available: true,
  levels: [
    {
      board: 6,
      count: 1,
      members_truncated: false,
      members: [
        {
          symbol: '002724', name: '海洋王', board_days: 6, pct: 10.02, price: 13.28,
          seal_amount_wan: 5000, first_seal: '09:25:00', open_times: 0,
          industry: '软件服务', circ_mv_yi: 17,
        },
      ],
    },
    {
      board: 1,
      count: 78,
      members_truncated: true,
      members: [],
    },
  ],
  detail_returned: 1,
  detail_partial: true,
  detail_note: '明细为采集时 Top40(连板高度↓+首封时间↑), 首板家数多时低位层级明细不全; 家数以标量列/梯队计数为准',
  promotion: {
    available: true,
    promo_rate_pct: 31.39,
    prev_date: '20261022',
    prev_count: 86,
    promoted: 27,
    sample_count: 86,
    sample_note: '样本 = 上一交易日东财涨停池家数(同口径同池)',
    baseline_available: false,
    baseline_note: '表内未存同池无条件基准(口径缺口)',
    excludes_one_word: false,
    one_word_note: '未剔除 T+1 一字涨停(口径缺口)',
    one_word_count: 3,
    per_level_available: false,
    per_level_note: '分层晋级率(首板→2板→…)表内未存逐层晋级明细, 不做前端推算',
  },
  prev_comparison: { available: true, prev_date: '20261022', delta_limit_up: -4, delta_broken: 2, delta_limit_down: 1 },
  phase: 'main_up',
  phase_label: '主升',
  emotion_available: true,
  emotion_basis_days: 12,
  cross_check: { eastmoney_zt: 82, tq_pool: 86, tq_max_streak: 6, diff: 4, note: '差北交所', tq_available: true },
  seal_mv_ratio: null,
  seal_mv_ratio_note: '封单额/流通市值比表内未存, 不做换算编造; 逐股封单额见成员明细',
  updated_at: '2026-10-23T15:52:00',
}

const SERIES = {
  caliber: 'eastmoney_zt',
  caliber_note: '东财三池同源同日(不含北交所); 与 TQ 全A自算口径并存不混算',
  days: 3,
  count: 3,
  items: [
    {
      trade_date: '20261021', trade_date_iso: '2026-10-21', available: true,
      limit_up_count: 90, broken_count: 10, limit_down_count: 3, break_rate_pct: 10.0,
      seal_rate_pct: 90.0, promo_rate_pct: 40.0, promo_prev_count: 100, max_streak: 5,
      first_board_count: 80, ge2_count: 8, ge3_count: 4, one_word_count: 2,
      phase: 'main_up', phase_label: '主升', quality: 'ok', source: 'eastmoney_replay',
      caliber: 'eastmoney_zt', delta_limit_up: 6, delta_broken: -2, delta_break_rate_pp: -1.2,
      delta_limit_down: 1, delta_max_streak: 1, delta_ge2: 2, prev_date: '20261020',
    },
    {
      trade_date: '20261022', trade_date_iso: '2026-10-22', available: false,
      limit_up_count: null, broken_count: null, limit_down_count: null, break_rate_pct: null,
      seal_rate_pct: null, promo_rate_pct: null, promo_prev_count: null, max_streak: null,
      first_board_count: null, ge2_count: null, ge3_count: null, one_word_count: null,
      phase: null, phase_label: null, quality: null, source: null, caliber: null,
      delta_limit_up: null, delta_broken: null, delta_break_rate_pp: null, delta_limit_down: null,
      delta_max_streak: null, delta_ge2: null, prev_date: null,
    },
    {
      trade_date: '20261023', trade_date_iso: '2026-10-23', available: true,
      limit_up_count: 82, broken_count: 12, limit_down_count: 2, break_rate_pct: 12.77,
      seal_rate_pct: 87.23, promo_rate_pct: 31.39, promo_prev_count: 86, max_streak: 6,
      first_board_count: 78, ge2_count: 3, ge3_count: 3, one_word_count: 3,
      phase: 'main_up', phase_label: '主升', quality: 'ok', source: 'eastmoney_replay',
      caliber: 'eastmoney_zt', delta_limit_up: null, delta_broken: null, delta_break_rate_pp: null,
      delta_limit_down: null, delta_max_streak: null, delta_ge2: null, prev_date: null,
    },
  ],
  note: null,
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ReplayTreePage />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  treeMock.mockReset()
  seriesMock.mockReset()
  treeMock.mockResolvedValue(TREE)
  seriesMock.mockResolvedValue(SERIES)
})

afterEach(() => {
  cleanup()
})

describe('连板晋级树页', () => {
  it('渲染梯队列: 首板在左→高板在右, 列头家数 + 成员明细(封单额走亿/万)', async () => {
    renderPage()
    await waitFor(() => expect(treeMock).toHaveBeenCalledWith(undefined))
    expect(await screen.findByTestId('replay-tree-root')).toBeTruthy()
    const txt = (screen.getByTestId('replay-tree-root').textContent) || ''
    expect(txt).toContain('首板')
    expect(txt).toContain('6板')
    expect(txt).toContain('78 家')
    // 成员: 名称/首封/封单(5000万→自动折算)/炸板次数
    expect(txt).toContain('海洋王')
    expect(txt).toContain('首封 09:25:00')
    expect(txt).toContain('5000.00万')
    expect(txt).toContain('炸 0次')
  })

  it('晋级率三件套: 样本 n=86 + 同池基准(口径缺口) + 未剔除一字板', async () => {
    renderPage()
    const promo = await screen.findByTestId('replay-tree-promotion')
    const txt = promo.textContent || ''
    expect(txt).toContain('31.39%')
    expect(txt).toContain('n=86')
    expect(txt).toContain('表内未存(口径缺口)')
    expect(txt).toContain('未剔除一字板(口径缺口)')
    expect(txt).toContain('续板 27')
  })

  it('明细截断: 空明细列标注「明细未落库(仅采集 Top40)」+ 顶部 Top40 说明(REPTREE-004)', async () => {
    renderPage()
    const root = await screen.findByTestId('replay-tree-root')
    const txt = root.textContent || ''
    expect(txt).toContain('明细未落库(仅采集 Top40)')
    expect(screen.getByTestId('replay-tree-detail-note').textContent).toContain('Top40')
    expect(screen.getByTestId('replay-tree-detail-note').textContent).toContain('家数以标量列/梯队计数为准')
  })

  it('双口径同屏: eastmoney_zt 与 tq_pool 各自带标签 + 差 4 家北交所(REPTREE-003)', async () => {
    renderPage()
    const cc = await screen.findByTestId('replay-tree-cross-check')
    const txt = cc.textContent || ''
    expect(txt).toContain('eastmoney_zt')
    expect(txt).toContain('tq_pool')
    expect(txt).toContain('86')
    expect(txt).toContain('4')
    // 标签上有 TQ 说明 tooltip
    expect(cc.innerHTML).toContain('全A自算')
  })

  it('封单额/流通市值比表内未存 → 显式「无数据」, 不现算(REPTREE-004)', async () => {
    renderPage()
    await screen.findByTestId('replay-tree-root')
    const gaps = screen.getByTestId('replay-tree-gaps')
    expect(gaps.textContent).toContain('封单额/流通市值比')
    expect(gaps.textContent).toContain('无数据')
    expect(gaps.textContent).toContain('表内未存')
  })

  it('分层晋级率不现算, 口径缺口显式(REPTREE-005)', async () => {
    renderPage()
    await screen.findByTestId('replay-tree-root')
    expect(screen.getByText('分层晋级率(首板→2板→…)表内未存逐层晋级明细, 不做前端推算')).toBeTruthy()
  })

  it('序列表: 缺落库日渲染(未落库)与 --, 不补 0(REPTREE-001); 点行切换树日期', async () => {
    renderPage()
    const missingRow = await screen.findByTestId('replay-series-row-20261022')
    const rowTxt = missingRow.textContent || ''
    expect(rowTxt).toContain('未落库')
    expect(rowTxt).toContain('--')
    expect(rowTxt).not.toContain('0 家')
    // 点行 → 用该日期重新拉树
    fireEvent.click(missingRow)
    await waitFor(() => expect(treeMock).toHaveBeenCalledWith('20261022'))
  })

  it('无快照: available=false → 提示 + 最近一期 + 查看按钮(REPTREE-006)', async () => {
    treeMock.mockResolvedValue({
      available: false,
      note: '该日期无复盘快照(20261012)',
      latest_date: '20261023',
      latest_date_iso: '2026-10-23',
      caliber: 'eastmoney_zt',
    })
    renderPage()
    expect(await screen.findByTestId('replay-tree-unavailable')).toBeTruthy()
    expect(screen.getByText('该日期无复盘快照(20261012)')).toBeTruthy()
    expect(screen.getByText('2026-10-23')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '查看最近一期' }))
    await waitFor(() => expect(treeMock).toHaveBeenCalledWith('20261023'))
  })

  it('payload 缺失 → 梯队显式无数据, 标量概览照常(REPTREE-004)', async () => {
    treeMock.mockResolvedValue({
      ...TREE,
      ladder_available: false,
      levels: [],
      detail_partial: false,
      detail_note: null,
    })
    renderPage()
    expect(await screen.findByTestId('replay-tree-missing')).toBeTruthy()
    const txt = screen.getByTestId('replay-tree-missing').textContent || ''
    expect(txt).toContain('无数据')
  })
})

describe('ReplayPromotionTree 组件', () => {
  const levels = [
    { board: 6, count: 1, members_truncated: false, members: [TREE.levels[0].members[0]] },
    { board: 1, count: 78, members_truncated: true, members: [] },
    { board: 3, count: 3, members_truncated: true, members: [] },
  ]

  it('传入乱序 levels 也按 首板→3板→6板 升序渲染(组件自己排序)', () => {
    const { container } = render(
      <ReplayPromotionTree levels={levels} ladderAvailable maxStreak={6} detailNote={null} />,
    )
    const txt = container.textContent || ''
    const iFirst = txt.indexOf('首板')
    const iB3 = txt.indexOf('3板')
    const iB6 = txt.indexOf('6板')
    expect(iFirst).toBeGreaterThanOrEqual(0)
    expect(iFirst).toBeLessThan(iB3)
    expect(iB3).toBeLessThan(iB6)
  })

  it('ladderAvailable=false → 显式无数据, 不用标量反推', () => {
    render(
      <ReplayPromotionTree levels={[]} ladderAvailable={false} maxStreak={6} detailNote={null} />,
    )
    const el = screen.getByTestId('replay-tree-missing')
    expect(el.textContent).toContain('无数据')
  })

  it('非空但截断的层级: 成员照常渲染 + 尾注「家数以 N 家为准」', () => {
    const truncated = [
      {
        board: 2,
        count: 9,
        members_truncated: true,
        members: [TREE.levels[0].members[0]],
      },
    ]
    const { container } = render(
      <ReplayPromotionTree levels={truncated} ladderAvailable maxStreak={6} detailNote={null} />,
    )
    const txt = container.textContent || ''
    expect(txt).toContain('海洋王')
    expect(txt).toContain('明细不全(采集 Top40)')
    expect(txt).toContain('家数以 9 家为准')
  })
})
