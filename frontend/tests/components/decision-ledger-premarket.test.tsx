// @vitest-environment jsdom
/**
 * 决策账本 · 盘前分析区块(2026-11-11)UI 契约。
 *
 * 这个区块回答「盘前报告的潜伏表说了什么、后来怎样」, 守五件事:
 *  ① 正常弹道: 按日期分块 + 该期汇总(潜伏 N 只 / 当日上涨 x/y / T+1 上涨 a/b) + 行(代码/名称/强度/埋伏方式/当日/T+1/T+3/止损);
 *  ② `parsed=false` → 明说「该期报告未解析到潜伏表」, 不出空表冒充;
 *  ③ `available=false` → 明说「暂无盘前分析记录」, 不出空表冒充;
 *  ④ 展开行显示真实信号明细; demon/price 为 null → 显「无数据」(**绝不补 0**);
 *  ⑤ 接口报错只降级本区块(页面其他区块照常), 且不用 role=alert 抢页面级错误态。
 *
 * 真数据纪律: mock 的是网络层, 组件走真实代码, 不编造后端数值。
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  stats: vi.fn(),
  log: vi.fn(),
  backtest: vi.fn(),
  entryOutcomes: vi.fn(),
  hitRate: vi.fn(),
  thresholds: vi.fn(),
  watchlistQuality: vi.fn(),
  premarket: vi.fn(),
}))

vi.mock('@sida/api', () => ({
  decisionsApi: {
    stats: (...a: unknown[]) => mocks.stats(...a),
    log: (...a: unknown[]) => mocks.log(...a),
    backtest: (...a: unknown[]) => mocks.backtest(...a),
    entryOutcomes: (...a: unknown[]) => mocks.entryOutcomes(...a),
    thresholds: (...a: unknown[]) => mocks.thresholds(...a),
    watchlistQuality: (...a: unknown[]) => mocks.watchlistQuality(...a),
    premarket: (...a: unknown[]) => mocks.premarket(...a),
  },
  signalsReviewApi: { hitRate: (...a: unknown[]) => mocks.hitRate(...a) },
  kindLabel: (k: string) => k,
}))

import DecisionLedger from '@/pages/DecisionLedger'

/** 一行潜伏表(字段与 `GET /api/decisions/premarket` 契约一致) */
function row(over: Record<string, unknown> = {}) {
  return {
    code: '600183',
    name: '生益科技',
    tags: ['妖股先锋·极妖'],
    event: '覆铜板提价',
    sector: '覆铜板',
    logic: '上游涨价传导, 龙头先受益',
    strength: 'A',
    method: '回调埋伏',
    batch: '首批1/3',
    stop: '跌破10日线或-5%',
    stop_price: null,
    target: '+10%',
    falsify: '提价落空',
    signals: {
      demon: { score: 78.1, grade: '妖', n_sealed: 12, max_streak: 4, participation: '高度参与', factor_date: '2026-11-10' },
      price: { close: 12.2, close_date: '2026-11-10', pct: 0.83, limit_up_streak: 0, pct_20d: 8.3, from_20d_high: -1.2, note: '盘前口径: 截至 2026-11-10 收盘(报告日 2026-11-11 收盘不计入)' },
      demon_note: null,
    },
    outcome: { t0: 5.0, t1: -2.86, t3: null, stop_breached: null, note: '止损按收盘价判定' },
    ...over,
  }
}

/** 一期盘前分析 */
function item(over: Record<string, unknown> = {}) {
  const rows = (over.rows as unknown[]) ?? [row()]
  return {
    date: '2026-11-11',
    record_id: 123,
    title: '【盘前分析】事件驱动前瞻·双轨制',
    quality: 'ok',
    parsed: true,
    row_count: rows.length,
    pit: {
      price_before: '2026-11-11',
      price_close_dates: ['2026-11-10'],
      demon_before: '2026-11-11',
      demon_evidence: true,
      demon_frozen_at: '2026-11-11T08:20:00',
      demon_reason: null,
      demon_asof: '2026-11-10',
      demon_available: rows.filter((r) => !!(r as { signals?: { demon?: unknown } }).signals?.demon).length,
      demon_unavailable: rows.filter((r) => !(r as { signals?: { demon?: unknown } }).signals?.demon).length,
    },
    summary: { rows: rows.length, t0_scored: rows.length, t0_up: 1, t1_scored: 1, t1_up: 0 },
    rows,
    ...over,
  }
}

const available = (items: unknown[]) => ({ available: true, days: 5, as_of: '2026-11-11T08:40:00+08:00', items, note: '潜伏表由 AI 撰写, 本页只解析并用真实行情对账。' })

beforeEach(() => {
  vi.clearAllMocks()
  mocks.stats.mockResolvedValue({ since: '2026-04-01', min_sample: 30, rows: [], note: '' })
  mocks.log.mockResolvedValue({ count: 0, total: 0, offset: 0, limit: 20, has_more: false, items: [], note: '' })
  mocks.hitRate.mockResolvedValue({ days: 30, by_type: {} })
  mocks.entryOutcomes.mockResolvedValue({ window_days: 30, min_sample: 20, available: true, rows: [], note: '' })
  mocks.thresholds.mockResolvedValue({ items: [], note: '' })
  mocks.watchlistQuality.mockResolvedValue({ available: false, user_id: 'u1', days: 180, min_sample: 30, symbols_count: 0, signals: [], error: '自选池为空', note: '' })
  mocks.premarket.mockResolvedValue(available([item()]))
})

afterEach(() => cleanup())

describe('DecisionLedger · 盘前分析区块', () => {
  it('正常渲染: 日期 + 该期汇总 + 潜伏表行(代码/名称/强度/埋伏方式/当日/T+1/T+3)', async () => {
    // 两期, 故意把旧的一期放前面 —— 页面必须按日期倒序
    mocks.premarket.mockResolvedValue(
      available([
        item({ date: '2026-11-10', record_id: 100, summary: { rows: 1, t0_scored: 1, t0_up: 1, t1_scored: 1, t1_up: 1 } }),
        item(),
      ]),
    )
    render(<DecisionLedger />)

    const sec = await screen.findByTestId('premarket-ledger')
    await waitFor(() => expect(within(sec).getAllByTestId('premarket-period')).toHaveLength(2))

    const periods = within(sec).getAllByTestId('premarket-period')
    expect(periods[0].textContent).toContain('2026-11-11') // 倒序: 最新在前
    expect(periods[1].textContent).toContain('2026-11-10')
    // 该期汇总口径
    expect(periods[0].textContent).toContain('潜伏 1 只')
    expect(periods[0].textContent).toContain('当日上涨 1/1')
    expect(periods[0].textContent).toContain('T+1 上涨 0/1')
    const pit = within(periods[0]).getByTestId('premarket-pit')
    expect(pit.textContent).toContain('盘前取数: 收盘 < 2026-11-11')
    expect(pit.textContent).toContain('实际 2026-11-10')
    expect(pit.textContent).toContain('事后表现')
    expect(pit.textContent).not.toContain('无生成时冻结快照')

    const rows = within(sec).getAllByTestId('premarket-row')
    expect(rows).toHaveLength(2)
    const r = rows[0]
    expect(r.textContent).toContain('600183')
    expect(r.textContent).toContain('生益科技')
    expect(r.textContent).toContain('妖股先锋·极妖') // tags 弱化小标签
    expect(r.textContent).toContain('A')
    expect(r.textContent).toContain('回调埋伏')
    expect(r.textContent).toContain('+5.00%') // 当日真实收益
    expect(r.textContent).toContain('-2.86%') // T+1
    expect(r.textContent).toContain('--') // T+3 = null → '--', 不补 0
    // 条件式止损: 显示原文 + 「条件式未判定」, 不假装已判
    expect(r.textContent).toContain('跌破10日线或-5%')
    expect(r.textContent).toContain('条件式未判定')
    // 取的是本页接口, 且带参数
    expect(mocks.premarket).toHaveBeenCalledWith(5, 30)
  })

  it('具体止损价位: 按 outcome.stop_breached 显「已破」/「未破」', async () => {
    mocks.premarket.mockResolvedValue(
      available([
        item({
          rows: [
            row({ code: '000001', name: '平安银行', stop: '跌破 11.50', stop_price: 11.5, outcome: { t0: -1.2, t1: null, t3: null, stop_breached: true, note: '止损按收盘价判定' } }),
            row({ code: '000002', name: '万科A', stop: '跌破 7.00', stop_price: 7, outcome: { t0: 1.2, t1: null, t3: null, stop_breached: false, note: null } }),
          ],
        }),
      ]),
    )
    render(<DecisionLedger />)
    const sec = await screen.findByTestId('premarket-ledger')
    await waitFor(() => expect(within(sec).getAllByTestId('premarket-row')).toHaveLength(2))
    const [broken, held] = within(sec).getAllByTestId('premarket-row')
    expect(broken.textContent).toContain('11.5')
    expect(broken.textContent).toContain('已破')
    expect(held.textContent).toContain('未破')
    expect(broken.textContent).not.toContain('条件式未判定')
  })

  it('parsed=false: 明说「该期报告未解析到潜伏表」, 不出空表冒充', async () => {
    mocks.premarket.mockResolvedValue(available([item({ parsed: false, rows: [], row_count: 0, summary: { rows: 0, t0_scored: 0, t0_up: 0, t1_scored: 0, t1_up: 0 } })]))
    render(<DecisionLedger />)
    const sec = await screen.findByTestId('premarket-ledger')
    await waitFor(() => expect(within(sec).getByTestId('premarket-unparsed')).toBeTruthy())
    expect(within(sec).getByTestId('premarket-unparsed').textContent).toContain('该期报告未解析到潜伏表')
    expect(within(sec).queryAllByTestId('premarket-row')).toHaveLength(0)
    expect(within(sec).queryByRole('table')).toBeNull()
  })

  it('available=false: 明说「暂无盘前分析记录」, 不出空表冒充', async () => {
    mocks.premarket.mockResolvedValue({ available: false, days: 5, as_of: '2026-11-11T08:40:00+08:00', items: [], note: '' })
    render(<DecisionLedger />)
    const sec = await screen.findByTestId('premarket-ledger')
    await waitFor(() => expect(within(sec).getByTestId('premarket-empty')).toBeTruthy())
    expect(within(sec).getByTestId('premarket-empty').textContent).toContain('暂无盘前分析记录')
    expect(within(sec).queryByRole('table')).toBeNull()
  })

  it('展开行: 显示受益逻辑/事件/板块/止损/目标/证伪 + 真实信号; 缺失显示「无数据」不补 0', async () => {
    mocks.premarket.mockResolvedValue(
      available([
        item({
          rows: [
            row(),
            row({
              code: '300750',
              name: '宁德时代',
              tags: [],
              signals: { demon: null, price: null, demon_note: '该期报告未保存生成时的股性因子冻结快照, 无盘前凭证' }, // 信号取不到 → 「无数据」
              outcome: { t0: null, t1: null, t3: null, stop_breached: null, note: '该交易日无K线(休市/停牌)' },
            }),
          ],
        }),
      ]),
    )
    render(<DecisionLedger />)
    const sec = await screen.findByTestId('premarket-ledger')
    await waitFor(() => expect(within(sec).getAllByTestId('premarket-row')).toHaveLength(2))

    // 缺信号的当日收益也不能变成 0
    const noDataRow = within(sec).getAllByTestId('premarket-row')[1]
    expect(noDataRow.textContent).not.toContain('0.00%')

    // 展开第一行(点「明细」按钮)
    fireEvent.click(within(within(sec).getAllByTestId('premarket-row')[0]).getByText('明细'))
    await waitFor(() => expect(within(sec).getAllByTestId('premarket-row-detail')).toHaveLength(1))
    const d1 = within(sec).getAllByTestId('premarket-row-detail')[0]
    expect(d1.textContent).toContain('覆铜板提价')
    expect(d1.textContent).toContain('覆铜板')
    expect(d1.textContent).toContain('上游涨价传导')
    expect(d1.textContent).toContain('跌破10日线或-5%')
    expect(d1.textContent).toContain('+10%')
    expect(d1.textContent).toContain('提价落空')
    expect(d1.textContent).toContain('78.1') // 股性评分真实数字
    expect(d1.textContent).toContain('封板 12 次')
    expect(d1.textContent).toContain('最高 4 连板')
    expect(d1.textContent).toContain('12.2') // 收盘价
    // 盘前口径必须标真实收盘日 + 快照日, 且明确事后表现
    expect(d1.textContent).toContain('2026-11-10')
    expect(d1.textContent).toContain('快照日 2026-11-10')
    expect(d1.textContent).toContain('盘前')
    expect(d1.textContent).toContain('事后')

    // 再展开缺信号那行 → 两处「无数据」+ 不可得原因, 且没有把 null 渲染成 0
    fireEvent.click(within(within(sec).getAllByTestId('premarket-row')[1]).getByText('明细'))
    await waitFor(() => expect(within(sec).getAllByTestId('premarket-row-detail')).toHaveLength(2))
    const d2 = within(sec).getAllByTestId('premarket-row-detail')[1]
    expect(d2.textContent).toContain('无数据')
    expect(d2.textContent).toContain('无盘前凭证')
    expect(d2.textContent).toContain('该交易日无K线(休市/停牌)')
    expect(d2.textContent).not.toContain('0.00')
  })

  it('无生成时冻结凭证: 明说股性按盘前不可得留空, 不拿当前因子表反推', async () => {
    mocks.premarket.mockResolvedValue(
      available([
        item({
          pit: {
            price_before: '2026-11-11',
            price_close_dates: ['2026-11-10'],
            demon_before: '2026-11-11',
            demon_evidence: false,
            demon_frozen_at: null,
            demon_reason: '该期报告未保存生成时的股性因子冻结快照, 无盘前凭证, 不展示',
            demon_asof: null,
            demon_available: 0,
            demon_unavailable: 1,
          },
          rows: [
            row({
              signals: {
                demon: null,
                price: null,
                demon_note: '该期报告未保存生成时的股性因子冻结快照, 无盘前凭证',
              },
            }),
          ],
        }),
      ]),
    )
    render(<DecisionLedger />)
    const sec = await screen.findByTestId('premarket-ledger')
    await waitFor(() => expect(within(sec).getAllByTestId('premarket-row')).toHaveLength(1))
    expect(within(sec).getByTestId('premarket-pit').textContent).toContain('无生成时冻结快照')

    fireEvent.click(within(within(sec).getAllByTestId('premarket-row')[0]).getByText('明细'))
    await waitFor(() => expect(within(sec).getAllByTestId('premarket-row-detail')).toHaveLength(1))
    const d = within(sec).getAllByTestId('premarket-row-detail')[0]
    expect(d.textContent).toContain('无数据')
    expect(d.textContent).toContain('无盘前凭证')
    expect(d.textContent).not.toContain('0.00')
  })

  it('接口报错只降级本区块: 显示一行「盘前分析加载失败」+ 重试, 其他区块照常且不抢 role=alert', async () => {
    mocks.premarket.mockRejectedValueOnce(Object.assign(new Error('502 Bad Gateway'), { kind: 'HTTP_5xx', status: 502 }))
    render(<DecisionLedger />)

    const sec = await screen.findByTestId('premarket-ledger')
    await waitFor(() => expect(within(sec).getByTestId('premarket-error')).toBeTruthy())
    expect(within(sec).getByTestId('premarket-error').textContent).toContain('盘前分析加载失败')
    expect(within(sec).getByText('重试')).toBeTruthy()
    // 本区块不得占用页面级错误态
    expect(screen.queryByRole('alert')).toBeNull()

    // 页面其余区块与核心取数照常
    expect(screen.getByTestId('thresholds')).toBeTruthy()
    expect(screen.getByTestId('signal-reconcile')).toBeTruthy()
    expect(screen.getByText('命中率')).toBeTruthy()

    // 重试成功 → 失败行消失, 数据上屏
    mocks.premarket.mockResolvedValueOnce(available([item()]))
    fireEvent.click(within(sec).getByText('重试'))
    await waitFor(() => expect(within(sec).queryByTestId('premarket-error')).toBeNull())
    expect(within(sec).getAllByTestId('premarket-row')).toHaveLength(1)
  })
})
