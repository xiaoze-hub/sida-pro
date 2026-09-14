// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 带1 `HeaderBand` 守两件事:
 *
 * ① **刷新**语义(Task 7 复审 Finding 1, 控制器裁定「页面级刷新」):
 *   点刷新 = 本带行情重取(`tick` → 重新发 `GET /quotes/{s}`, **个股**) **且**回调 `onRefresh`
 *   —— 页面(`StockWorkbench`)靠它把正文子树(指数/板块正文、个股带2)重挂载 ⇒ 重新取数;
 *   `onRefresh` 可选: 不传时行为与旧版一致(只刷自身, 不抛)。
 *
 * ② **同码不同标的闸门**(Important, 控制器裁定): `type !== 'stock'`(指数/板块)**不发**
 *   `GET /quotes/{s}`, 也**不渲染**其名称/现价/涨跌(顶行)与整条快照行 —— 同一代码在个股面与
 *   指数/板块面是**不同标的**(`000001`: 指数 = 上证指数, 个股 = 平安银行 11.74 -0.93%)。
 *   带1 对非个股只给类型三按钮 + 刷新 + 裸代码中性标签; 名称与数值由正文 `IndexBody`/`BoardBody`
 *   拥有。故个股 fixture 刻意用**另一标的**(平安银行)的真值: 组件若在 `type=index|board` 下
 *   画了它, 断言立刻抓到泄漏。
 *
 * 说明: 页面测试(`stock-workbench.test.tsx`)把本组件 mock 掉了, 故"真组件是否真的调了
 * `onRefresh`"/"是否真的没发 /quotes"必须由本文件守 —— 否则漏调/误发, 页面测试仍会全绿。
 * 真数据纪律: 本文件 mock 的是**网络层**(`@panwatch/api`), 组件取数/渲染走真实代码。
 */

const mocks = vi.hoisted(() => ({
  fetchAPI: vi.fn(),
  quote: vi.fn(),
  moreInfo: vi.fn(),
  klineSummary: vi.fn(),
}))

vi.mock('@panwatch/api', () => ({
  fetchAPI: (...args: unknown[]) => mocks.fetchAPI(...args),
  insightApi: {
    quote: (...args: unknown[]) => mocks.quote(...args),
    moreInfo: (...args: unknown[]) => mocks.moreInfo(...args),
    klineSummary: (...args: unknown[]) => mocks.klineSummary(...args),
  },
}))

import HeaderBand from '@panwatch/biz-ui/components/workbench/HeaderBand'

/**
 * 个股(000001)真值: **平安银行** —— 与 `?type=index` 的 `000001`(上证指数)**不同标的**。
 * 顶行/快照行若在非个股下渲染出这些数, 即"同码不同标的"泄漏(本测试的核心断言)。
 */
const STOCK_QUOTE = {
  name: '平安银行',
  current_price: 11.74,
  change_pct: -0.93,
  open_price: 11.82,
  high_price: 11.86,
}

beforeEach(() => {
  mocks.fetchAPI.mockResolvedValue({})
  mocks.quote.mockResolvedValue(STOCK_QUOTE)
  mocks.moreInfo.mockResolvedValue({})
  mocks.klineSummary.mockResolvedValue(null)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const refreshButton = () => screen.getByRole('button', { name: '刷新' })

describe('HeaderBand 刷新(页面级)', () => {
  it('个股: 点刷新 → 自身行情重取 + 广播 onRefresh(正文据此重挂载重取数)', async () => {
    const onRefresh = vi.fn()
    render(<HeaderBand symbol="000001" market="CN" type="stock" onRefresh={onRefresh} />)

    // 挂载即取数
    await waitFor(() => expect(mocks.quote).toHaveBeenCalledTimes(1))
    expect(mocks.quote).toHaveBeenLastCalledWith('000001', 'CN')
    expect(onRefresh).not.toHaveBeenCalled()

    fireEvent.click(refreshButton())

    // ① 自身行情重取
    await waitFor(() => expect(mocks.quote).toHaveBeenCalledTimes(2))
    // ② 页面级广播**恰好一次**(每次点击一次; 不多不少)
    expect(onRefresh).toHaveBeenCalledTimes(1)

    fireEvent.click(refreshButton())
    await waitFor(() => expect(mocks.quote).toHaveBeenCalledTimes(3))
    expect(onRefresh).toHaveBeenCalledTimes(2)
  })

  it('个股 不传 onRefresh: 仍只刷自身行情(向后兼容, 不抛)', async () => {
    render(<HeaderBand symbol="000001" market="CN" type="stock" />)
    await waitFor(() => expect(mocks.quote).toHaveBeenCalledTimes(1))

    fireEvent.click(refreshButton())
    await waitFor(() => expect(mocks.quote).toHaveBeenCalledTimes(2))
  })
})

describe('HeaderBand 同码不同标的闸门: 指数/板块不发 /quotes, 不渲染个股名称/价格', () => {
  it('type=index: 一次都不发 /quotes|more-info|l2|klineSummary, 只给裸代码 + 类型按钮/刷新', async () => {
    render(<HeaderBand symbol="000001" market="CN" type="index" />)
    // 挂载后给微任务一轮机会(若误发请求, 这里就能被看到) —— 断言"零取数"
    await waitFor(() => expect(screen.getByRole('group', { name: '类型切换' })).toBeTruthy())
    expect(mocks.quote).not.toHaveBeenCalled()
    expect(mocks.moreInfo).not.toHaveBeenCalled()
    expect(mocks.fetchAPI).not.toHaveBeenCalled()
    expect(mocks.klineSummary).not.toHaveBeenCalled()

    // **不渲染**个股的名称/现价/涨跌(平安银行 11.74 -0.93% 属另一标的)
    expect(screen.queryByText('平安银行')).toBeNull()
    expect(screen.queryByText('11.74')).toBeNull()
    expect(screen.queryByText('-0.93%')).toBeNull()
    // **不渲染**个股快照行(今开 11.82 / 最高 11.86 …)
    expect(screen.queryByText('今开')).toBeNull()
    expect(screen.queryByText('11.82')).toBeNull()
    expect(screen.queryByText('最高')).toBeNull()

    // 保留: 裸代码中性标签(非 quote) + 类型三按钮(指数选中) + 刷新
    expect(screen.getByText('000001')).toBeTruthy()
    const switchGroup = screen.getByRole('group', { name: '类型切换' })
    expect(switchGroup.textContent).toContain('个股')
    expect(switchGroup.textContent).toContain('板块')
    expect(screen.getByRole('button', { name: '指数' }).getAttribute('aria-pressed')).toBe('true')
    expect(refreshButton()).toBeTruthy()
  })

  it('type=board: 同样零取数、不渲染个股名称/价格/快照行', async () => {
    render(<HeaderBand symbol="880001" market="CN" type="board" />)
    await waitFor(() => expect(screen.getByRole('group', { name: '类型切换' })).toBeTruthy())
    expect(mocks.quote).not.toHaveBeenCalled()
    expect(mocks.moreInfo).not.toHaveBeenCalled()
    expect(mocks.fetchAPI).not.toHaveBeenCalled()
    expect(mocks.klineSummary).not.toHaveBeenCalled()

    expect(screen.queryByText('平安银行')).toBeNull()
    expect(screen.queryByText('11.74')).toBeNull()
    expect(screen.queryByText('-0.93%')).toBeNull()
    expect(screen.queryByText('今开')).toBeNull()

    expect(screen.getByText('880001')).toBeTruthy()
    expect(screen.getByRole('button', { name: '板块' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('type=index 点刷新: 仍广播 onRefresh(正文重取), 但绝不因此发 /quotes', async () => {
    const onRefresh = vi.fn()
    render(<HeaderBand symbol="000001" market="CN" type="index" onRefresh={onRefresh} />)

    fireEvent.click(refreshButton())
    expect(onRefresh).toHaveBeenCalledTimes(1)
    // 刷新仍不碰个股数据面
    expect(mocks.quote).not.toHaveBeenCalled()
    expect(mocks.moreInfo).not.toHaveBeenCalled()
    expect(mocks.fetchAPI).not.toHaveBeenCalled()

    fireEvent.click(refreshButton())
    expect(onRefresh).toHaveBeenCalledTimes(2)
    expect(mocks.quote).not.toHaveBeenCalled()
  })

  it('type=stock(对照): 名称/现价/涨跌/快照行都渲染, /quotes 恰好一次', async () => {
    render(<HeaderBand symbol="000001" market="CN" type="stock" />)
    await waitFor(() => expect(mocks.quote).toHaveBeenCalledTimes(1))

    expect(screen.getByText('平安银行')).toBeTruthy()
    expect(screen.getByText('11.74')).toBeTruthy()
    expect(screen.getByText('-0.93%')).toBeTruthy()
    // 快照行(今开 11.82)在个股下照旧渲染
    expect(screen.getByText('今开')).toBeTruthy()
    expect(screen.getByText('11.82')).toBeTruthy()
  })
})

/**
 * Task 19 (Finding 4): 「持仓态未知」标注的**真组件**守卫。
 *
 * `stock-workbench.test.tsx` 把 `HeaderBand` mock 掉了(它守的是页面接线), 于是带1 里
 * `position-unknown` 那个 span 的 JSX 若被改坏(删了/改了条件), 页面测试**照样全绿**。
 * 本文件渲染**真组件** + mock 网络层, 直接断言可见文案与 testid 的存在/缺席。
 *
 * 语义: `positionUnknown` 为 `true` 时(真实持仓源在途/失败)显示「持仓态未知」;
 * 为 `false`(已判定持仓/未持仓)时**不显示** —— 不把已判定态说成未知。
 */
describe('HeaderBand 持仓态未知(Task 19: 真组件, 可见文案)', () => {
  /** 让建议条渲染的最小 summary(klineSummary 非空 ⇒ buildKlineSuggestion 出建议条)。 */
  const SUMMARY = {
    timeframe: '1d',
    asof: '2026-09-11',
    trend: '多头排列',
    ma5: 11.5,
    ma10: 11.2,
    ma20: 11.0,
    macd_status: '金叉',
    macd_hist: 0.12,
    rsi14: 55,
    volume_status: '放量',
  }

  it('positionUnknown=true: 真组件渲染可见「持仓态未知」(testid position-unknown)', async () => {
    mocks.klineSummary.mockResolvedValue(SUMMARY)
    render(<HeaderBand symbol="002636" market="CN" type="stock" hasPosition={false} positionUnknown />)
    // 等建议条落定(未知标注挂在建议条旁)
    await waitFor(() => expect(screen.getByTestId('position-unknown')).toBeTruthy())
    expect(screen.getByTestId('position-unknown').textContent).toBe('持仓态未知')
    expect(screen.getByText('持仓态未知')).toBeTruthy()
  })

  it('positionUnknown=false(已判定未持仓): 真组件**不**渲染「持仓态未知」', async () => {
    mocks.klineSummary.mockResolvedValue(SUMMARY)
    render(<HeaderBand symbol="002636" market="CN" type="stock" hasPosition={false} />)
    await waitFor(() => expect(mocks.klineSummary).toHaveBeenCalled())
    // 先证建议条确实渲染了(不是"整条没渲染"导致的空过)
    await waitFor(() => expect(screen.getByText(/建议·/)).toBeTruthy())
    expect(screen.queryByTestId('position-unknown')).toBeNull()
    expect(screen.queryByText('持仓态未知')).toBeNull()
  })

  it('positionUnknown=true 但 summary 未落定: 建议条未出 ⇒ 未知标注也不在(不孤立挂载)', async () => {
    mocks.klineSummary.mockResolvedValue(null)
    render(<HeaderBand symbol="002636" market="CN" type="stock" hasPosition={false} positionUnknown />)
    await waitFor(() => expect(mocks.klineSummary).toHaveBeenCalled())
    expect(screen.queryByTestId('position-unknown')).toBeNull()
  })
})

/**
 * 遗留⑤: 快照行补 **成交量 / 振幅 / 封单额**(spec §1.2 明列, `DATA_OWNERSHIP` 把它们都判给
 * `band1.snapshot`)。守的是**接线**(纯函数口径由 `tests/lib/workbench-snapshot.test.ts` 守):
 *  ① 三格的值来自带1 **本来就在打**的两个端点 —— `/quotes/{s}`(volume/high/low/prev_close)与
 *     `/stocks/{s}/l2`(`more.fcamo` + `snapshot`), 且 `/l2` **仍只有一条请求**(snapshot 与 more 同源);
 *  ② 缺字段 → `--`(不编造, 不当 0);
 *  ③ 封单额是个股专属 cell: `type=index` 时整条快照行(含三格)都不渲染 —— 右栏「盘口速览」的
 *     封单行已删, 带1 是全站唯一拥有面(去重断言见 `workbench-dedup-audit.test.tsx` ⑦)。
 */
describe('HeaderBand 快照行补 成交量/振幅/封单额(遗留⑤)', () => {
  const L2_FULL = {
    snapshot: { now: 82.46, last_close: 77.4, high: 83.2, low: 76.5, volume: 12345 },
    more: {
      zt_price: 84.1,
      fcamo: 812_000_000, // 元(后端已把 FCAmo 万元 ×1e4)
      pe_dynamic: 39.19,
      pe_ttm: 60.26,
      pb: 14,
      dividend_yield: 1.23,
      ever_zt_count: 2,
    },
  }

  beforeEach(() => {
    mocks.quote.mockResolvedValue({
      name: '金安国纪',
      current_price: 82.46,
      change_pct: 7.86,
      open_price: 77.0,
      high_price: 83.2,
      low_price: 76.5,
      prev_close: 77.4,
      volume: 1_234_567, // 手
      turnover: 987_654_321,
    })
    mocks.fetchAPI.mockImplementation(async (url: unknown) => {
      if (String(url).includes('/l2')) return L2_FULL
      return {}
    })
  })

  it('三格真值落位: 成交量 123.46万手 / 振幅 8.66% / 封单额 8.12亿, 且 /l2 仍只发一条', async () => {
    render(<HeaderBand symbol="002636" market="CN" type="stock" />)

    await waitFor(() => expect(screen.getByText('成交量')).toBeTruthy())
    // 成交量: /quotes.volume(手)→ 万手
    expect(screen.getByText('123.46万手')).toBeTruthy()
    // 振幅 = (83.2 − 76.5) / 77.4 × 100 = 8.66%(三值同源自 /quotes)
    expect(screen.getByText('振幅')).toBeTruthy()
    expect(screen.getByText('8.66%')).toBeTruthy()
    // 封单额: /l2 的 more.fcamo(元)→ 8.12亿
    expect(screen.getByText('封单额')).toBeTruthy()
    expect(screen.getByText('8.12亿')).toBeTruthy()

    // **未新增请求**: snapshot 与 more 取自同一条 `/l2` 响应
    const l2Calls = mocks.fetchAPI.mock.calls.filter((c) => String(c[0]).includes('/l2'))
    expect(l2Calls).toHaveLength(1)
    expect(mocks.quote).toHaveBeenCalledTimes(1)
  })

  it('缺字段 → 三格 `--`(quote 无 volume/prev_close, /l2 无 snapshot 与 fcamo)', async () => {
    mocks.quote.mockResolvedValue({ name: '金安国纪', current_price: 82.46, change_pct: 7.86, high_price: 83.2, low_price: 76.5 })
    mocks.fetchAPI.mockImplementation(async (url: unknown) => {
      if (String(url).includes('/l2')) return { more: { zt_price: 84.1 } }
      return {}
    })
    render(<HeaderBand symbol="002636" market="CN" type="stock" />)

    await waitFor(() => expect(screen.getByText('封单额')).toBeTruthy())
    // /quotes 三值缺失 ⇒ 振幅不出数(缺值守卫), 成交量/封单额同样 `--`(不因"有别的源到了"就误填)。
    // 注: 本用例的 `/l2` 夹具**没有 `snapshot` 段**, 故它**检不出**"跨源拼数"(如 quotes.high 配
    // l2.last_close) —— 那条同源纪律由 `tests/lib/workbench-snapshot.test.ts` 的
    // 「振幅回退: … 绝不跨源拼数」用例守(复审 Minor 6: 原注释把功劳记错了地方)。
    expect(screen.getByText('振幅').parentElement?.textContent).toBe('振幅--')
    expect(screen.getByText('成交量').parentElement?.textContent).toBe('成交量--')
    expect(screen.getByText('封单额').parentElement?.textContent).toBe('封单额--')
    // 涨停价(同一条 /l2 的 more)照旧渲染 ⇒ 证明不是"整条 /l2 没到"造成的空过
    expect(screen.getByText('84.1')).toBeTruthy()
  })

  it('type=index: 整条快照行不渲染(含三格), 且一个数据面都不发', async () => {
    render(<HeaderBand symbol="000001" market="CN" type="index" />)
    await waitFor(() => expect(screen.getByRole('group', { name: '类型切换' })).toBeTruthy())

    expect(screen.queryByText('成交量')).toBeNull()
    expect(screen.queryByText('振幅')).toBeNull()
    expect(screen.queryByText('封单额')).toBeNull()
    expect(screen.queryByText('8.12亿')).toBeNull()
    expect(mocks.quote).not.toHaveBeenCalled()
    expect(mocks.fetchAPI).not.toHaveBeenCalled()
  })
})

/**
 * KI-059 方案 B(2026-09-18): 封单额等 `/l2` 读数在带1 **无 30s 轮询** —— 盘中封单额可能
 * 秒级剧变但屏上不动。方案 B = **不轮询**, 在快照行尾显式标「快照 HH:MM:SS」(取数时刻),
 * 不把首屏时刻伪装成实时。`as_of` 缺失则不渲染时钟(不编时间)。
 */
describe('HeaderBand 快照时钟(KI-059 方案 B)', () => {
  beforeEach(() => {
    mocks.quote.mockResolvedValue({
      name: '金安国纪',
      current_price: 82.46,
      change_pct: 7.86,
    })
    mocks.fetchAPI.mockImplementation(async (url: unknown) => {
      if (String(url).includes('/l2')) {
        return {
          as_of: '2026-09-18T10:23:45+08:00',
          more: { zt_price: 84.1, fcamo: 812_000_000 },
          snapshot: {},
        }
      }
      return {}
    })
  })

  it('/l2 带 as_of → 快照行尾渲染「快照 10:23:45」, title 说明无轮询', async () => {
    render(<HeaderBand symbol="002636" market="CN" type="stock" />)
    await waitFor(() => expect(screen.getByTestId('band1-l2-snapshot-clock')).toBeTruthy())
    const el = screen.getByTestId('band1-l2-snapshot-clock')
    expect(el.textContent).toBe('快照 10:23:45')
    expect(el.getAttribute('title') || '').toContain('无 30s 轮询')
    // 封单额仍在(时钟是附加披露, 不替代数值)
    expect(screen.getByText('8.12亿')).toBeTruthy()
  })

  it('/l2 缺 as_of → 不渲染时钟(不编时间)', async () => {
    mocks.fetchAPI.mockImplementation(async (url: unknown) => {
      if (String(url).includes('/l2')) {
        return { more: { zt_price: 84.1, fcamo: 812_000_000 }, snapshot: {} }
      }
      return {}
    })
    render(<HeaderBand symbol="002636" market="CN" type="stock" />)
    await waitFor(() => expect(screen.getByText('封单额')).toBeTruthy())
    expect(screen.queryByTestId('band1-l2-snapshot-clock')).toBeNull()
  })

  it('type=index: 无 /l2 请求 ⇒ 无快照时钟', async () => {
    render(<HeaderBand symbol="000001" market="CN" type="index" />)
    await waitFor(() => expect(screen.getByRole('group', { name: '类型切换' })).toBeTruthy())
    expect(screen.queryByTestId('band1-l2-snapshot-clock')).toBeNull()
    expect(mocks.fetchAPI).not.toHaveBeenCalled()
  })
})
