// @vitest-environment jsdom
/**
 * 参谋回答页 · 证据溯源接线（2026-10-29 P1 第二步）。
 *
 * 上一步（4062cffb）只做了 `buildAnswerTrace` / `EvidenceTraceNote`，页面没接线。
 * 这一步把真实 `/ai-copilot` 页的 structured AnswerCard 接上，并且**只在能说清归属时**才挂：
 *
 *  1. 关联**只有一种**来源：提问文本匹配（`chat_messages` 无 run_id）。
 *     页面永远不传 `run-id`，也不从回答正文里捡 run_id 字符串自证身份 ——
 *     正文里出现 run_id 是模型写的字，不是服务端关联。
 *  2. 同一会话里同一个问题被问过多次：必须按「第几次出现」对齐 run（runs 按 id desc 返回）。
 *     旧实现 `runs.find` 永远命中最新那一轮 → 旧回答被挂上新 run，这是错轮。
 *     对不齐（候选不够 / 多出来）就回到「未关联」，宁可空着。
 *  3. 切会话：`useCopilotRuns` 在新会话请求回来前保留旧列表，文本又可能相同 ——
 *     候选 run 的 `conversation_id` 必须**明确等于**当前会话；缺失或他会话一律不挂。
 *  4. readiness 只认这条消息**自己**的流（定稿时固化的 `m.readiness`）；
 *     当前在跑的 stage 流属于正在写的那条草稿，**不许**挂到任何历史回答上。
 *  5. 草稿（还没终稿）不挂溯源条：身份/证据都还没定稿。
 *
 * 渲染真页面、走真 send 逻辑，只 mock 网络层（与 copilot-stream-race 同一套手法）。
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { CopilotRunRow, CopilotRunStep } from '../../src/lib/copilot/api'

const apiMock = vi.hoisted(() => ({
  uploadAttachment: vi.fn(),
  sendMessageStream: vi.fn(),
  createConversation: vi.fn(),
  getConversation: vi.fn(),
  listConversations: vi.fn(async () => []),
  fetchAPI: vi.fn(),
}))

const convMock = vi.hoisted(() => ({
  conversations: [
    { id: 9, title: '会话A', stock_symbol: null, created_at: '' },
    { id: 10, title: '会话B', stock_symbol: null, created_at: '' },
  ],
  list: vi.fn(async () => {}),
  rename: vi.fn(async () => ({ ok: true })),
  pin: vi.fn(async () => ({ ok: true })),
  remove: vi.fn(async () => ({ ok: true })),
}))

vi.mock('@sida/api', () => ({ chatApi: apiMock, fetchAPI: apiMock.fetchAPI }))
vi.mock('../../src/lib/copilot/api', () => ({
  getCopilotMetrics: vi.fn(async () => ({ days: 7, runs: 0, ok_rate: null, avg_tools: null, p95_elapsed_ms: null, by_recipe: [] })),
  getPendingActions: vi.fn(async () => []),
  applyPendingAction: vi.fn(async () => ({})),
  rejectPendingAction: vi.fn(async () => ({})),
  reportViewState: vi.fn(async () => ({ saved: true })),
  getCopilotPresets: vi.fn(async () => []),
  getCopilotBrief: vi.fn(async () => null),
  getCopilotMentions: vi.fn(async () => ({ stocks: [], agents: [] })),
}))
vi.mock('../../src/hooks/useConversations', () => ({ useConversations: () => convMock }))
vi.mock('../../src/components/SafeMarkdown', () => ({
  default: ({ children }: { children: string }) => <div data-testid="md-fallback">{children}</div>,
}))
vi.mock('../../src/components/copilot/CopilotBriefCard', () => ({
  CopilotBriefCard: () => <div data-testid="brief-card" />,
}))

import AiCopilotPage, { buildMessageAnswerTrace, resolveRunForMessage, type Msg } from '../../src/pages/AiCopilot'

/** 结构化回答（parseAnswer 能切出段落，才会走 AnswerCard）。 */
const answer = (tag: string, source = '腾讯逐笔口径') =>
  `【结论】${tag}：观望\n【依据】\n- 今日 -1.9%（${source}）\n【预案】继续保持观察\n【口径】普通行情\n以上分析不构成投资建议。`

const run = (runId: string, cid: number, question: string, status = 'ok'): CopilotRunRow => ({
  run_id: runId,
  conversation_id: cid,
  recipe: 'market_attribution',
  status,
  tool_calls: 2,
  elapsed_ms: 3200,
  question,
  created_at: '2026-10-29T09:00:00',
})

const stepsFor = (tools: string[]): CopilotRunStep[] =>
  tools.map((tool, i) => ({ seq: i + 1, tool, ok: true, elapsed_ms: 100, args: null, result_digest: null }))

/** 每次用例自定：会话 → messages；以及 /copilot/runs 的返回。 */
let convMessages: Record<number, { role: 'user' | 'assistant'; content: string }[]>
let runList: CopilotRunRow[]
/** 某些会话的 runs 请求一直不回来（模拟切会话瞬间旧列表还在）。 */
let hangingRunsFor: number[]
/** 每次用例自定：run 详情。 */
let details: Record<string, CopilotRunStep[]>
/** 发出去的新一轮（send 后 push 进来，用于收流）。 */
const pending: Array<{ handlers: any; resolve: (r?: unknown) => void; reject: (e: unknown) => void }> = []

function installFetchMock() {
  apiMock.fetchAPI.mockReset()
  apiMock.fetchAPI.mockImplementation((path: string) => {
    if (typeof path === 'string' && path.startsWith('/copilot/runs/')) {
      const rid = decodeURIComponent(path.slice('/copilot/runs/'.length))
      const steps = details[rid]
      if (!steps) return Promise.reject(new Error('404'))
      return Promise.resolve({
        run: { run_id: rid, status: 'ok', recipe: null, question: null, tool_calls: steps.length, elapsed_ms: 1, created_at: '' },
        steps,
      })
    }
    if (typeof path === 'string' && path.startsWith('/copilot/runs')) {
      const m = /conversation_id=(\d+)/.exec(path)
      const cid = m ? Number(m[1]) : null
      if (cid != null && hangingRunsFor.includes(cid)) return new Promise(() => {})
      // 真实后端按 conversation_id 过滤（这里照做，避免用"不过滤"掩盖越界）
      const rows = cid == null ? runList : runList.filter((r) => r.conversation_id === cid)
      return Promise.resolve(rows)
    }
    return Promise.resolve([])
  })
}

beforeEach(() => {
  Element.prototype.scrollTo = (() => {}) as unknown as typeof Element.prototype.scrollTo
  pending.length = 0
  convMessages = {}
  runList = []
  details = {}
  hangingRunsFor = []
  apiMock.fetchAPI.mockReset()
  installFetchMock()
  apiMock.getConversation.mockReset()
  apiMock.getConversation.mockImplementation(async (id: number) => {
    if (!convMessages[id]) throw new Error('no conversation')
    return { conversation: { id, title: `会话${id}`, stock_symbol: null, created_at: '' }, messages: convMessages[id] }
  })
  apiMock.sendMessageStream.mockReset()
  apiMock.sendMessageStream.mockImplementation(
    (_id: number, _q: string, handlers: any) =>
      new Promise((resolve, reject) => {
        pending.push({ handlers, resolve, reject })
      }),
  )
  apiMock.createConversation.mockReset()
  apiMock.createConversation.mockResolvedValue({ id: 9, title: '' })
  window.history.pushState({}, '', '/ai-copilot')
})

afterEach(() => {
  cleanup()
})

async function openConversation(id: number) {
  fireEvent.click(screen.getByTestId(`copilot-conv-title-${id}`))
  await waitFor(() => expect(screen.getByTestId(`copilot-conv-title-${id}`).getAttribute('aria-current')).toBe('true'))
}

async function ask(question: string) {
  fireEvent.change(screen.getByPlaceholderText(/问点什么|补充说明/), { target: { value: question } })
  await act(async () => {
    fireEvent.click(screen.getByLabelText('发送'))
  })
  await act(async () => {
    await Promise.resolve()
  })
}

const traces = () => screen.queryAllByTestId('copilot-evidence-trace')

describe('⓪ 关联解析：没有明确归属就返回 null', () => {
  const msgs: Msg[] = [
    { role: 'user', content: '看看 600111' },
    { role: 'assistant', content: answer('第一轮') },
  ]

  it('没有会话 id → 不归属任何 run', () => {
    const runs = [run('run-x', 9, '看看 600111')]
    expect(resolveRunForMessage(msgs, runs, 1, null)).toBeNull()
  })

  it('run 的 conversation_id 为空/为他会话 → 不归属（无法证明是本会话这一轮）', () => {
    const orphan = [{ ...run('run-null', 9, '看看 600111'), conversation_id: null }]
    expect(resolveRunForMessage(msgs, orphan, 1, 9)).toBeNull()
    expect(resolveRunForMessage(msgs, [run('run-other', 10, '看看 600111')], 1, 9)).toBeNull()
  })

  it('草稿（live）不出溯源：终稿未到就没有可核验的答案', () => {
    const runs = [run('run-x', 9, '看看 600111')]
    expect(
      buildMessageAnswerTrace({
        msgs,
        runs,
        details: {},
        index: 1,
        conversationId: 9,
        live: true,
        readiness: null,
      }),
    ).toBeNull()
  })

  it('即使能匹配到 run，association 也只可能是 question-text（页面永不自称 run-id）', () => {
    const runs = [run('run-x', 9, '看看 600111')]
    const trace = buildMessageAnswerTrace({
      msgs,
      runs,
      details: {},
      index: 1,
      conversationId: 9,
      live: false,
      readiness: null,
    })
    expect(trace?.association).toBe('question-text')
    expect(trace?.verified).toBe(false)
  })
})

describe('① run 关联：页面只按提问文本匹配，永不自证 run-id', () => {
  it('文本匹配成功 → 标「未验证」，并写明逐句来源不可查', async () => {
    convMessages[9] = [
      { role: 'user', content: '看看 600111' },
      { role: 'assistant', content: answer('第一轮') },
    ]
    runList = [run('run-a1', 9, '看看 600111')]
    details = { 'run-a1': stepsFor(['get_stock_quote']) }

    render(<AiCopilotPage />)
    await openConversation(9)
    await waitFor(() => expect(screen.getByText(/第一轮/)).toBeTruthy())

    const notes = traces()
    expect(notes.length).toBe(1)
    expect(notes[0].getAttribute('data-association')).toBe('question-text')
    expect(notes[0].getAttribute('data-verified')).toBe('false')
    expect(notes[0].textContent).toContain('run-a1')
    expect(notes[0].textContent).toContain('未验证')
    // 逐句来源：必须可见地写「无」
    expect(notes[0].textContent).toContain('无逐句来源')
    // 绝不能出现服务端关联口径
    expect(notes[0].textContent).not.toContain('按 run_id 关联')
  })

  it('回答正文里写了 run_id 字符串也不能自证关联（无对应 run 就未关联）', async () => {
    convMessages[9] = [
      { role: 'user', content: '看看 600111' },
      { role: 'assistant', content: `${answer('自报轨迹')}（本轮 run_id: run-fake-999）` },
    ]
    runList = []

    render(<AiCopilotPage />)
    await openConversation(9)
    await waitFor(() => expect(screen.getByText(/自报轨迹/)).toBeTruthy())

    const notes = traces()
    expect(notes.length).toBe(1)
    expect(notes[0].getAttribute('data-association')).toBe('none')
    expect(notes[0].getAttribute('data-verified')).toBe('false')
    expect(notes[0].textContent).toContain('未关联')
    expect(notes[0].textContent).not.toContain('已核验')
    // 正文里的 run_id 不被拿来当关联凭证
    expect(notes[0].textContent).not.toContain('run-fake-999')
  })

  it('步骤只在「候选 run 自己」的详情已加载时才出现；未加载不写 0 步', async () => {
    convMessages[9] = [
      { role: 'user', content: '看看 600111' },
      { role: 'assistant', content: answer('第一轮') },
    ]
    runList = [run('run-a1', 9, '看看 600111')]
    details = { 'run-a1': stepsFor(['get_stock_quote', 'get_delta_series']) }

    render(<AiCopilotPage />)
    await openConversation(9)
    await waitFor(() => expect(screen.getByText(/第一轮/)).toBeTruthy())

    // 详情还没展开 → 步骤未加载，工具行不出现，也不写 0 步（run.tool_calls=2 不许冒充明细）
    const before = traces()[0]
    expect(before.textContent).toContain('步骤未加载')
    expect(before.textContent).not.toContain('2 步')
    expect(screen.queryByTestId('evidence-trace-tools')).toBeNull()

    // 展开这一轮的「过程」→ 真的拉了 run-a1 详情 → 溯源条列出真实工具
    fireEvent.click(screen.getByTestId('inline-steps-toggle'))
    await waitFor(() => expect(traces()[0].textContent).toContain('get_stock_quote'))
    const after = traces()[0]
    expect(after.textContent).toContain('2 步')
    expect(after.textContent).toContain('get_delta_series')
  })
})

describe('② 同一问题不同轮次：按出现次序对齐，不许都挂最新那一轮', () => {
  it('两轮同问 → 旧回答挂旧 run、新回答挂新 run', async () => {
    convMessages[9] = [
      { role: 'user', content: '看看 600111' },
      { role: 'assistant', content: answer('第一轮') },
      { role: 'user', content: '看看 600111' },
      { role: 'assistant', content: answer('第二轮') },
    ]
    // 后端 id desc：新 → 旧
    runList = [run('run-a2', 9, '看看 600111'), run('run-a1', 9, '看看 600111')]

    render(<AiCopilotPage />)
    await openConversation(9)
    await waitFor(() => expect(screen.getByText(/第二轮/)).toBeTruthy())

    const notes = traces()
    expect(notes.length).toBe(2)
    expect(notes[0].textContent).toContain('run-a1')
    expect(notes[0].textContent).not.toContain('run-a2')
    expect(notes[1].textContent).toContain('run-a2')
    expect(notes[1].textContent).not.toContain('run-a1')
    // 内联步骤条与溯源条必须是同一轮（不能一个说 run-a1 一个说 run-a2）
    const inline = screen.getAllByTestId('inline-steps')
    expect(inline.length).toBe(2)
  })

  it('候选 run 少于同问次数（有一轮没落库）→ 多出来的那条不挂，回未关联', async () => {
    convMessages[9] = [
      { role: 'user', content: '看看 600111' },
      { role: 'assistant', content: answer('第一轮') },
      { role: 'user', content: '看看 600111' },
      { role: 'assistant', content: answer('第二轮') },
    ]
    runList = [run('run-a2', 9, '看看 600111')] // 只有最新一轮有记录

    render(<AiCopilotPage />)
    await openConversation(9)
    await waitFor(() => expect(screen.getByText(/第二轮/)).toBeTruthy())

    const notes = traces()
    expect(notes[1].textContent).toContain('run-a2')
    expect(notes[0].textContent).toContain('未关联')
    expect(notes[0].textContent).not.toContain('run-a2')
  })
})

describe('③ 会话切换：旧会话的 runs 还在缓存里时不许串到新会话', () => {
  it('新会话 runs 请求未回来 + 文本相同 → 一律未关联，不挂旧会话的 run', async () => {
    convMessages[9] = [
      { role: 'user', content: '看看 600111' },
      { role: 'assistant', content: answer('会话A回答') },
    ]
    convMessages[10] = [
      { role: 'user', content: '看看 600111' },
      { role: 'assistant', content: answer('会话B回答') },
    ]
    runList = [run('run-a', 9, '看看 600111')]
    // 打开 B 时，runs 请求挂着不返回 → hook 里仍是会话 A 的列表
    hangingRunsFor = [10]

    render(<AiCopilotPage />)
    await openConversation(9)
    await waitFor(() => expect(screen.getByText(/会话A回答/)).toBeTruthy())
    expect(traces()[0].textContent).toContain('run-a')

    await openConversation(10)
    await waitFor(() => expect(screen.getByText(/会话B回答/)).toBeTruthy())
    const notes = traces()
    expect(notes.length).toBe(1)
    expect(notes[0].textContent).toContain('未关联')
    expect(notes[0].textContent).not.toContain('run-a')
  })
})

describe('④ 无证据：说清没有，不补 0', () => {
  it('没有 run、也没有取数痕迹 → 未关联 + 无取数痕迹（不出现 0）', async () => {
    convMessages[9] = [
      { role: 'user', content: '看看 600111' },
      { role: 'assistant', content: answer('第一轮') },
    ]
    runList = []

    render(<AiCopilotPage />)
    await openConversation(9)
    await waitFor(() => expect(screen.getByText(/第一轮/)).toBeTruthy())

    const note = traces()[0]
    expect(note.getAttribute('data-association')).toBe('none')
    expect(note.textContent).toContain('未关联到运行记录')
    expect(note.textContent).toContain('无取数痕迹')
    expect(note.textContent).not.toContain('0 步')
    expect(note.textContent).not.toContain('未取到 0')
  })
})

describe('⑤ 流式取数不串线：历史回答不挂当前流的 stage 状态', () => {
  it('正在跑的新一轮有未取到，历史回答的缺口仍是「无取数痕迹」；草稿不挂溯源条', async () => {
    convMessages[9] = [
      { role: 'user', content: '看看 600111' },
      { role: 'assistant', content: answer('历史回答') },
    ]
    runList = [run('run-a1', 9, '看看 600111')]
    // 同票追问才应复用历史会话；未知绑定会按安全契约新建会话。
    apiMock.getConversation.mockImplementationOnce(async (id: number) => ({
      conversation: { id, title: `会话${id}`, stock_symbol: '600111', created_at: '' },
      messages: convMessages[id],
    }))

    render(<AiCopilotPage />)
    await openConversation(9)
    await waitFor(() => expect(screen.getByText(/历史回答/)).toBeTruthy())
    expect(traces().length).toBe(1)

    await ask('600111 还能追吗')
    await waitFor(() => expect(pending.length).toBe(1))
    await act(async () => {
      pending[0].handlers.onStage('正在取数 2/3：get_delta_series 失败')
      pending[0].handlers.onDelta('【结论】草稿')
    })

    // 齐套度条属于正在跑的这一轮（有失败的 pill）
    expect(screen.getByTestId('evidence-readiness')).toBeTruthy()
    // 草稿还没终稿 → 不挂溯源条；历史回答仍然是它自己的「无取数痕迹」
    const notes = traces()
    expect(notes.length).toBe(1)
    expect(notes[0].textContent).toContain('无取数痕迹')
    expect(notes[0].textContent).not.toContain('未取到 1')

    // 终稿 + 服务端 run 落库后：新回答只带自己那一轮的缺口数字
    runList = [run('run-a2', 9, '600111 还能追吗'), run('run-a1', 9, '看看 600111')]
    await act(async () => {
      pending[0].handlers.onDone({ id: 33, role: 'assistant', content: answer('新回答'), created_at: '' })
      pending[0].resolve({ content: answer('新回答'), authoritative: true, corruptedFrames: 0, errors: [] })
    })
    await waitFor(() => expect(screen.getByText(/新回答/)).toBeTruthy())

    await waitFor(() => expect(traces().length).toBe(2))
    const after = traces()
    expect(after[0].textContent).toContain('无取数痕迹') // 历史回答：仍无
    expect(after[1].textContent).toContain('run-a2')
    expect(after[1].textContent).toContain('未取到 1') // 自己的流里那条失败证据
  })
})

describe('⑥ 依据来源角标：可见地标「模型自述·未核验」，不只靠 title', () => {
  it('角标正文里就有未核验字样', async () => {
    convMessages[9] = [
      { role: 'user', content: '看看 600111' },
      { role: 'assistant', content: answer('第一轮', '腾讯逐笔口径') },
    ]
    runList = []

    render(<AiCopilotPage />)
    await openConversation(9)
    await waitFor(() => expect(screen.getByText(/第一轮/)).toBeTruthy())

    const chips = screen.getAllByTestId('copilot-evidence-source')
    expect(chips.length).toBeGreaterThan(0)
    for (const chip of chips) {
      expect(chip.textContent).toContain('模型自述')
      expect(chip.textContent).toContain('未核验')
    }
  })
})
