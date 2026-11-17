// @vitest-environment jsdom
/**
 * 当前 K 线快照 → 数智参谋（按轮发送）· 端到端（2026-11-17）。
 *
 * 这一条链路的每一段都必须是真的：
 *   图（`KlineChart.onChanSnapshotChange` 回调）
 *     → 工作台把那份**已落图**快照发布到 `lib/copilot/chart-context` 的内存仓库
 *     → 参谋页每轮发送时**现取**，作为 `chart_snapshot` 放进这一轮请求体
 *     → 请求体之外的地方（`initial_context` / 正文 / 消息 / 本地存储）一个字都不留。
 *
 * 所以这里渲染**真实 StockWorkbench + 真实 AiCopilot 同时在场**（参谋在工作台上以面板形态
 * 打开就是这种拓扑），只把 K 线画布换成一个能"点火上报"的替身 —— 指针落在真页面的 props 上，
 * 不是把 mock 的调用当成接线。
 *
 * 负面用例逐条对应硬约束：
 *   ② 换标的 → 旧图当场撤下，下一轮不带；
 *   ③ 用户清除 → 撤下且不复活（工作台再广播同一只票也被拒）；
 *   ④ 超过 TTL → 界面上说"本轮不发送"，请求里真的没有；
 *   ⑤ 换账号 → 上一个账号的快照读不到、发不出去；
 *   ⑥ `createConversation` 的 `await` 期间图变了 → 发送那一刻重新读，不拿旧值。
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom'

const DIGEST_A = 'a'.repeat(64)
const DIGEST_B = 'b'.repeat(64)
const OWNER = 'test-user'
const OTHER_OWNER = 'another-user'

const mocks = vi.hoisted(() => ({
  fetchAPI: vi.fn(),
  portfolioSummary: vi.fn(),
  chat: {
    sendMessageStream: vi.fn(),
    createConversation: vi.fn(),
    listConversations: vi.fn(async () => []),
    getConversation: vi.fn(),
    deleteConversation: vi.fn(),
  },
}))

vi.mock('@sida/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@sida/api')>()
  return {
    ...actual,
    fetchAPI: mocks.fetchAPI,
    chatApi: mocks.chat,
    dashboardApi: { ...actual.dashboardApi, portfolioSummary: mocks.portfolioSummary },
  }
})

/**
 * K 线替身：把"图表上报"变成一个真实的用户动作（点按钮 = 组件回调父组件的 prop），
 * 父组件（StockWorkbench）那边走的是真代码路径。
 */
vi.mock('@sida/biz-ui/components/KlineChart', async () => {
  function KlineStub(p: {
    symbol: string
    market: string
    initialInterval?: string
    onChanSnapshotChange?: (s: unknown) => void
    onIntervalChange?: (i: string) => void
  }) {
    const snap = (digest: string, days = 250) => ({
      symbol: p.symbol,
      market: p.market,
      instrument: 'stock',
      interval: '1d',
      days,
      digest,
    })
    return (
      <div data-testid="kline" data-symbol={`${p.symbol}:${p.market}`}>
        <button type="button" onClick={() => p.onChanSnapshotChange?.(snap(DIGEST_A))}>
          chart-emit-a
        </button>
        <button type="button" onClick={() => p.onChanSnapshotChange?.(snap(DIGEST_B, 120))}>
          chart-emit-b
        </button>
        <button type="button" onClick={() => p.onChanSnapshotChange?.(null)}>
          chart-emit-null
        </button>
        <button type="button" onClick={() => p.onIntervalChange?.('1w')}>
          mock-to-week
        </button>
      </div>
    )
  }
  return { default: KlineStub }
})

vi.mock('@/hooks/useWorkbenchAnnotations', () => ({
  useWorkbenchAnnotations: () => ({ snapshot: { key: '', phase: 'ready', lines: [], markers: [] }, priceLines: undefined }),
}))
vi.mock('@/hooks/useCopilotBrief', () => ({ useCopilotBrief: () => ({ data: null }) }))
vi.mock('@/hooks/useSourceHealth', () => ({
  useSourceHealth: () => ({ health: {}, loading: false, isReady: () => true, reasonOf: () => '' }),
}))
vi.mock('@sida/biz-ui/components/workbench/HeaderBand', () => ({
  default: (p: { symbol: string }) => <div data-testid="band1">{p.symbol}</div>,
}))
vi.mock('@sida/biz-ui/components/workbench/QuickRail', () => ({ default: () => <div data-testid="rail" /> }))
vi.mock('@/pages/workbench/IndexBody', () => ({ default: () => <div data-testid="index-body" /> }))
vi.mock('@sida/biz-ui/components/workbench/BoardBody', () => ({ default: () => <div data-testid="board-body" /> }))
vi.mock('@/components/DisciplineCoolDownBanner', () => ({ DisciplineCoolDownBanner: () => null }))
vi.mock('@/components/DefenseLineBanner', () => ({ DefenseLineBanner: () => null }))
vi.mock('@sida/biz-ui/components/Fund4Strip', () => ({ default: () => null }))
vi.mock('@/components/RangeStatsCard', () => ({ default: () => <div data-testid="range-stats" /> }))
vi.mock('@/components/copilot/AnnotationLinkBar', () => ({ AnnotationLinkBar: () => null }))

function tabMock(id: string) {
  return async () => ({ default: () => <div data-testid={`tab-${id}`} /> })
}
vi.mock('@/pages/workbench/tabs/L2Tab', tabMock('l2'))
vi.mock('@/pages/workbench/tabs/SuggestTab', tabMock('suggest'))
vi.mock('@/pages/workbench/tabs/FundamentalTab', tabMock('fundamental'))
vi.mock('@/pages/workbench/tabs/NewsTab', tabMock('news'))
vi.mock('@/pages/workbench/tabs/ResearchTab', tabMock('research'))
vi.mock('@/pages/workbench/tabs/ForecastTab', tabMock('forecast'))

vi.mock('@/lib/copilot/api', () => ({
  getCopilotMetrics: vi.fn(async () => ({ days: 7, runs: 0, ok_rate: null, avg_tools: null, p95_elapsed_ms: null, by_recipe: [] })),
  getPendingActions: vi.fn(async () => []),
  applyPendingAction: vi.fn(async () => ({})),
  rejectPendingAction: vi.fn(async () => ({})),
  reportViewState: vi.fn(async () => ({ saved: true })),
  getCopilotPresets: vi.fn(async () => []),
  getCopilotBrief: vi.fn(async () => null),
  getCopilotMentions: vi.fn(async () => ({ stocks: [], agents: [] })),
}))
vi.mock('@/hooks/useConversations', () => ({
  useConversations: () => ({
    conversations: [],
    list: vi.fn(async () => {}),
    rename: vi.fn(async () => ({ ok: true })),
    pin: vi.fn(async () => ({ ok: true })),
    remove: vi.fn(async () => ({ ok: true })),
  }),
}))
vi.mock('@/components/SafeMarkdown', () => ({
  default: ({ children }: { children: string }) => <div data-testid="md-fallback">{children}</div>,
}))
vi.mock('@/components/copilot/CopilotBriefCard', () => ({ CopilotBriefCard: () => <div data-testid="brief-card" /> }))
vi.mock('@/components/copilot/LiveRunPanel', () => ({ LiveRunPanel: () => <div data-testid="live-run-panel" /> }))

import StockWorkbench from '@/pages/StockWorkbench'
import AiCopilotPage from '@/pages/AiCopilot'
import {
  CHAN_SNAPSHOT_SCHEMA,
  CHAN_SNAPSHOT_TTL_MS,
  chanSnapshotStoreSize,
  clearChanSnapshotStore,
  publishChanSnapshot,
  readChanSnapshotDetailed,
} from '@/lib/copilot/chart-context'
import { clearChartContextStore } from '@/lib/copilot/chart-context'

function Probe() {
  const navigate = useNavigate()
  return (
    <button type="button" onClick={() => navigate('/stocks/000776?period=d1')}>
      mock-go-other-symbol
    </button>
  )
}

/** 工作台 + 参谋同时在场（参谋以面板形态贴着图打开，就是生产里的拓扑）。 */
function renderBoth() {
  return render(
    <MemoryRouter initialEntries={['/stocks/600111?period=d1']}>
      <Probe />
      <Routes>
        <Route
          path="/stocks/:symbol"
          element={
            <>
              <StockWorkbench />
              <AiCopilotPage entry={{ id: 1, symbol: '600111', market: 'CN', pageContext: '用户从个股工作台进入。' }} />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  )
}

function composer() {
  return within(screen.getByTestId('copilot-composer'))
}

async function ask(question: string) {
  await waitFor(() => expect(composer().getByRole('textbox')).toBeTruthy())
  fireEvent.change(composer().getByRole('textbox'), { target: { value: question } })
  fireEvent.click(composer().getByLabelText('发送'))
}

/** 第 8 个参数就是本轮同图快照（不传 = 不带）。 */
function sentSnapshot(callIndex = 0) {
  return mocks.chat.sendMessageStream.mock.calls[callIndex]?.[7] ?? null
}

function setToken(sub: string) {
  localStorage.setItem('token', `e30.${btoa(JSON.stringify({ sub }))}.signature`)
}

beforeEach(() => {
  Element.prototype.scrollTo = (() => {}) as unknown as typeof Element.prototype.scrollTo
  setToken(OWNER)
  window.history.pushState({}, '', '/stocks/600111?period=d1')
  clearChanSnapshotStore()
  clearChartContextStore()
  mocks.fetchAPI.mockReset()
  mocks.fetchAPI.mockImplementation(async () => [])
  mocks.portfolioSummary.mockReset()
  mocks.portfolioSummary.mockResolvedValue({ accounts: [], total: {} })
  for (const fn of Object.values(mocks.chat)) (fn as { mockReset?: () => void }).mockReset?.()
  mocks.chat.createConversation.mockResolvedValue({ id: 21, title: 't', created_at: '' })
  mocks.chat.listConversations.mockResolvedValue([])
  mocks.chat.sendMessageStream.mockImplementation(async (_id: number, _q: string, handlers: any) => {
    handlers?.onDone?.({ id: 7, role: 'assistant', content: '收到。', created_at: '' })
    return { content: '收到。', authoritative: true, corruptedFrames: 0, errors: [] }
  })
})

afterEach(() => {
  cleanup()
  localStorage.removeItem('token')
  clearChanSnapshotStore()
  clearChartContextStore()
})

describe('主链路：图上那份快照真的进了这一轮请求体', () => {
  it('图表上报 → 界面可见 → 发送时随请求体带上，且不写会话/正文', async () => {
    renderBoth()
    await waitFor(() => expect(screen.getByTestId('kline')).toBeTruthy())

    // ① 图表上报（组件回调页面 prop）→ 工作台发布
    fireEvent.click(screen.getByText('chart-emit-a'))
    const line = await screen.findByTestId('copilot-chan-snapshot')
    expect(line.textContent).toContain('600111')
    expect(line.textContent).toContain('日线')
    expect(line.textContent).toContain('近 250 天')
    expect(line.textContent).toContain('摘要 aaaaaaaa…')
    // 完整摘要（指纹）不上界面
    expect(line.textContent).not.toContain(DIGEST_A)
    expect(line.textContent).toContain('不写入会话历史')

    // ② 用户真在参谋里问一句
    await ask('这个区间的笔怎么画')
    await waitFor(() => expect(mocks.chat.sendMessageStream).toHaveBeenCalled())

    // ③ 请求体里就是那一份（7 个协议字段）
    expect(sentSnapshot()).toEqual({
      schema: CHAN_SNAPSHOT_SCHEMA,
      symbol: '600111',
      market: 'CN',
      instrument: 'stock',
      interval: '1d',
      days: 250,
      digest: DIGEST_A,
    })

    // ④ 会话创建参数里没有任何摘要（不进 initial_context），正文里也没有
    const convPayload = mocks.chat.createConversation.mock.calls[0][0] as Record<string, unknown>
    expect(JSON.stringify(convPayload)).not.toContain(DIGEST_A)
    expect(String(mocks.chat.sendMessageStream.mock.calls[0][1])).not.toContain(DIGEST_A)
    // 也不进普通文本历史：用户气泡里只有用户那句话
    expect(screen.queryByText(new RegExp(DIGEST_A))).toBeNull()
  })
})

describe('切换即隔离', () => {
  it('换标的 → 旧图当场撤下，下一轮一个字符都不带', async () => {
    renderBoth()
    await waitFor(() => expect(screen.getByTestId('kline')).toBeTruthy())
    fireEvent.click(screen.getByText('chart-emit-a'))
    await screen.findByTestId('copilot-chan-snapshot')

    fireEvent.click(screen.getByText('mock-go-other-symbol'))
    await waitFor(() => expect(screen.getByTestId('kline').dataset.symbol).toBe('000776:CN'))
    // 600111 那份已经不在仓库里（不是"读的时候才过滤"）
    expect(readChanSnapshotDetailed('600111', 'CN', OWNER).block).toBeNull()
    await waitFor(() => expect(screen.queryByTestId('copilot-chan-snapshot')).toBeNull())

    await ask('那这只呢')
    await waitFor(() => expect(mocks.chat.sendMessageStream).toHaveBeenCalled())
    expect(sentSnapshot()).toBeNull()
  })

  it('图表回调 null（换周期/加载失败/卸载）→ 立刻撤下', async () => {
    renderBoth()
    await waitFor(() => expect(screen.getByTestId('kline')).toBeTruthy())
    fireEvent.click(screen.getByText('chart-emit-a'))
    await screen.findByTestId('copilot-chan-snapshot')
    fireEvent.click(screen.getByText('chart-emit-null'))
    await waitFor(() => expect(screen.queryByTestId('copilot-chan-snapshot')).toBeNull())
    await ask('还在吗')
    await waitFor(() => expect(mocks.chat.sendMessageStream).toHaveBeenCalled())
    expect(sentSnapshot()).toBeNull()
  })

  it('页面隐藏（切标签/最小化）→ 撤下，不拿"刚才那张"继续发', async () => {
    renderBoth()
    await waitFor(() => expect(screen.getByTestId('kline')).toBeTruthy())
    fireEvent.click(screen.getByText('chart-emit-a'))
    await screen.findByTestId('copilot-chan-snapshot')

    // jsdom 里 visibilityState 是只读的：用 defineProperty 模拟一次真实的隐藏事件
    const spy = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    spy.mockRestore()

    await waitFor(() => expect(screen.queryByTestId('copilot-chan-snapshot')).toBeNull())
    await ask('还在吗')
    await waitFor(() => expect(mocks.chat.sendMessageStream).toHaveBeenCalled())
    expect(sentSnapshot()).toBeNull()
  })
})

describe('用户清除 / 过期 / 换账号', () => {
  it('用户清除 → 条消失、本轮不带，且工作台再广播同一只票也不复活', async () => {
    renderBoth()
    await waitFor(() => expect(screen.getByTestId('kline')).toBeTruthy())
    fireEvent.click(screen.getByText('chart-emit-a'))
    await screen.findByTestId('copilot-chan-snapshot')

    fireEvent.click(screen.getByTestId('copilot-chan-snapshot-clear'))
    await waitFor(() => expect(screen.queryByTestId('copilot-chan-snapshot')).toBeNull())
    expect(chanSnapshotStoreSize()).toBe(0)

    // 图表再上报（甚至换了摘要）都不许复活
    fireEvent.click(screen.getByText('chart-emit-b'))
    await act(async () => {})
    expect(screen.queryByTestId('copilot-chan-snapshot')).toBeNull()
    expect(chanSnapshotStoreSize()).toBe(0)

    await ask('那还说吗')
    await waitFor(() => expect(mocks.chat.sendMessageStream).toHaveBeenCalled())
    expect(sentSnapshot()).toBeNull()
  })

  it('超过 2 分钟没有新的图表广播 → 界面说"本轮不发送"，请求里真的没有', async () => {
    // 仓库里放一条两分多钟前广播的记录（真实 store API，只把时刻拨到过去）
    publishChanSnapshot({
      owner: OWNER,
      symbol: '600111',
      market: 'CN',
      instrument: 'stock',
      interval: '1d',
      days: 250,
      digest: DIGEST_A,
      now: Date.now() - CHAN_SNAPSHOT_TTL_MS - 2_000,
    })
    renderBoth()
    const line = await screen.findByTestId('copilot-chan-snapshot')
    expect(line.textContent).toContain('本轮不发送')

    await ask('这条还能用吗')
    await waitFor(() => expect(mocks.chat.sendMessageStream).toHaveBeenCalled())
    expect(sentSnapshot()).toBeNull()
  })

  it('换账号 → 上一个账号的快照读不到也发不出去（记录仍在，只是不属于我）', async () => {
    publishChanSnapshot({
      owner: OTHER_OWNER,
      symbol: '600111',
      market: 'CN',
      instrument: 'stock',
      interval: '1d',
      days: 250,
      digest: DIGEST_A,
    })
    renderBoth()
    await waitFor(() => expect(screen.getByTestId('kline')).toBeTruthy())
    expect(screen.queryByTestId('copilot-chan-snapshot')).toBeNull()

    await ask('替我看看')
    await waitFor(() => expect(mocks.chat.sendMessageStream).toHaveBeenCalled())
    expect(sentSnapshot()).toBeNull()
    // 别人的记录没被误删，只是当前账号读不到
    expect(chanSnapshotStoreSize()).toBe(1)
  })
})

describe('会话本身不记快照（历史/追问都不靠记忆）', () => {
  it('同一会话第二轮：图还在就照带，图撤下就一个都不带', async () => {
    renderBoth()
    await waitFor(() => expect(screen.getByTestId('kline')).toBeTruthy())
    fireEvent.click(screen.getByText('chart-emit-a'))
    await screen.findByTestId('copilot-chan-snapshot')

    await ask('第一轮')
    await waitFor(() => expect(mocks.chat.sendMessageStream).toHaveBeenCalledTimes(1))
    expect(sentSnapshot(0)?.digest).toBe(DIGEST_A)

    // 图还在：第二轮照样带当前这一份
    await ask('第二轮')
    await waitFor(() => expect(mocks.chat.sendMessageStream).toHaveBeenCalledTimes(2))
    expect(sentSnapshot(1)?.digest).toBe(DIGEST_A)

    // 图撤下（用户切走/页面隐藏/组件说没图）：第三轮一个都不带 —— 会话里没有"记住的旧摘要"
    fireEvent.click(screen.getByText('chart-emit-null'))
    await waitFor(() => expect(screen.queryByTestId('copilot-chan-snapshot')).toBeNull())
    await ask('第三轮')
    await waitFor(() => expect(mocks.chat.sendMessageStream).toHaveBeenCalledTimes(3))
    expect(sentSnapshot(2)).toBeNull()
  })
})

describe('异步窗口：会话创建在飞时图变了', () => {
  it('createConversation 未回来时撤下快照 → 发送那一刻重新读，结果是"不带"', async () => {
    let release: (v: unknown) => void = () => {}
    mocks.chat.createConversation.mockImplementation(
      () => new Promise((resolve) => {
        release = resolve
      }),
    )
    renderBoth()
    await waitFor(() => expect(screen.getByTestId('kline')).toBeTruthy())
    fireEvent.click(screen.getByText('chart-emit-a'))
    await screen.findByTestId('copilot-chan-snapshot')

    await ask('这个笔怎么确认')
    await waitFor(() => expect(mocks.chat.createConversation).toHaveBeenCalled())

    // 会话还在建：用户把图切走了（组件回调 null）
    fireEvent.click(screen.getByText('chart-emit-null'))
    await act(async () => {
      release({ id: 33, title: 't', created_at: '' })
    })

    await waitFor(() => expect(mocks.chat.sendMessageStream).toHaveBeenCalled())
    expect(sentSnapshot()).toBeNull()
  })

  it('createConversation 未回来时换了摘要 → 发的是发送那一刻那份，不是发起时那份', async () => {
    let release: (v: unknown) => void = () => {}
    mocks.chat.createConversation.mockImplementation(
      () => new Promise((resolve) => {
        release = resolve
      }),
    )
    renderBoth()
    await waitFor(() => expect(screen.getByTestId('kline')).toBeTruthy())
    fireEvent.click(screen.getByText('chart-emit-a'))
    await screen.findByTestId('copilot-chan-snapshot')

    await ask('再确认一次')
    await waitFor(() => expect(mocks.chat.createConversation).toHaveBeenCalled())

    // 新数据到了 → 新摘要（120 天那份）
    fireEvent.click(screen.getByText('chart-emit-b'))
    await act(async () => {
      release({ id: 34, title: 't', created_at: '' })
    })

    await waitFor(() => expect(mocks.chat.sendMessageStream).toHaveBeenCalled())
    const sent = sentSnapshot()
    expect(sent?.digest).toBe(DIGEST_B)
    expect(sent?.days).toBe(120)
  })
})
