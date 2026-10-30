import { useViewportPanel } from '@/hooks/useViewportPanel'
import { AssistantAvatar } from '@/components/AssistantAvatar'
import type { AssistantEntry } from '@/components/AssistantWorkspace'
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { safeFixed } from '@/lib/format'
import {
  applyPendingAction,
  getCopilotMetrics,
  getPendingActions,
  rejectPendingAction,
  type CopilotRunDetail,
  type CopilotRunRow,
  type CopilotMetrics,
  type PendingAction,
} from '@/lib/copilot/api'
import { ChevronRight, Loader2, PanelLeftClose, PanelLeftOpen, Paperclip, Send, Sparkles, X } from 'lucide-react'
import { chatApi, type ChatStreamResult, type EvidenceRowAudit } from '@sida/api'
import SafeMarkdown from '@/components/SafeMarkdown'
import { AnswerCard } from '@/components/copilot/AnswerCard'
import InlineSteps from '@/components/copilot/InlineSteps'
import EvidenceRowAuditPanel from '@/components/copilot/EvidenceRowAuditPanel'
import { AmbientGrid } from '@/components/motion/AmbientGrid'
import { StreamingBeam } from '@/components/motion/StreamingBeam'
import { EvidenceReadinessStrip } from '@/components/copilot/EvidenceReadinessStrip'
import { buildAnswerTrace, type AnswerTrace } from '@/lib/copilot/evidence-provenance'
import {
  emptyReadiness,
  foldStageEvent,
  parseStageLabel,
  settleReadiness,
  type ReadinessState,
} from '@/lib/copilot/evidence-readiness'
import { anchorKindLabel, type PriceAnchorKind } from '@/lib/copilot/price-anchors'
import { CopilotBriefCard } from '@/components/copilot/CopilotBriefCard'
import { parseAnswer } from '@/lib/copilot/answer-blocks'
import { extractSymbolChips, marketOf, resolveTurnSymbol } from '@/lib/copilot/symbolChips'
import { publicAssistantContent } from '@/lib/copilot/public-content'
import { applyMention, detectMention, filterMentions, mentionKey, type MentionHit } from '@/lib/copilot/mentions'
import { getCopilotMentions, type MentionAgent, type MentionStock } from '@/lib/copilot/api'
import { MentionPicker, type MentionTab } from '@/components/copilot/MentionPicker'
import { ChartContextChip } from '@/components/copilot/ChartContextChip'
import {
  chartContextOwnerMatches,
  chartContextKey,
  chartContextPrompt,
  dismissChartContext,
  isChartContextFresh,
  readChartContextDetailed,
  unpublishChartContext,
  type ChartContext,
} from '@/lib/copilot/chart-context'
import { currentAccountId } from '@/lib/copilot/annotationLedger'
import { ConversationSidebar } from '@/components/copilot/ConversationSidebar'
import { MessageActions } from '@/components/copilot/MessageActions'
import { ToolTimeline } from '@/components/copilot/ToolTimeline'
import { MemoryPanel } from '@/components/copilot/MemoryPanel'
import { SchedulePanel } from '@/components/copilot/SchedulePanel'
import { ProposalPanel } from '@/components/copilot/ProposalPanel'
import { PersonaBadge } from '@/components/copilot/PersonaBadge'
import { ModelPicker } from '@/components/copilot/ModelPicker'
import { LiveRunPanel } from '@/components/copilot/LiveRunPanel'
import { RAIL_KEY, readBoolPref, resolveShortcut, writePref } from '@/lib/copilot/layout'
import { useConversations } from '@/hooks/useConversations'
import { useCopilotRuns } from '@/hooks/useCopilotRuns'
import {
  getCopilotRunStatus,
  isTerminalStatus,
  progressText,
  submitCopilotRun,
} from '@/lib/copilot/jobs'
import {
  ATTACH_CHIP_LABEL,
  ATTACH_EMPTY,
  ATTACH_FAILED,
  ATTACH_HINT,
  ATTACH_IMAGE_READY,
  ATTACH_PARSING,
  ATTACH_REMOVE,
  ATTACH_TOO_LARGE,
  CONTEXT_ANCHOR_CLEAR,
  CONTEXT_ANCHOR_PREFIX,
  PAGE_EMPTY_EXAMPLES,
  PAGE_EMPTY_HINT,
  SEND,
} from '@/lib/copilot/copy'

/** 与服务端 /api/chat/upload 上限保持一致（20MB）。 */
const MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024

/** 右侧面板 tab（顺序即 tab bar 顺序；'proposals' = 参谋能力缺口清单）。 */
const PANEL_TABS = ['runs', 'memory', 'schedule', 'proposals'] as const
type PanelTab = (typeof PANEL_TABS)[number]

/** tab bar 上的短名（列表口径）。 */
const PANEL_TAB_LABEL: Record<PanelTab, string> = {
  runs: '过程',
  memory: '记忆',
  schedule: '定时',
  proposals: '提案',
}

/** aside 标题（比 tab 短名多一层语义，用户一眼知道这一栏是什么）。 */
const PANEL_TAB_TITLE: Record<PanelTab, string> = {
  runs: '本轮过程',
  memory: '长期记忆',
  schedule: '定时任务',
  proposals: '能力提案',
}

/** 待发送附件：解析文本 + 可选图片 data URL（图片走多模态，文本模型走解析结果）。 */
interface PendingAttachment {
  name: string
  text: string
  imageData?: string
  isImage: boolean
}

/** 指令池接口失败时的本地兜底（只影响"有没有快捷按钮"，不影响能不能提问）。 */

export type Msg = {
  role: 'user' | 'assistant'
  content: string
  /** 服务端持久化的本轮 run；历史消息或未成功记录时为空。 */
  runId?: string | null
  /** 服务端落库的依据行数字核对（旧消息为 null，不补）。 */
  evidenceAudit?: EvidenceRowAudit | null
  /** 用户消息附带的文件名（气泡里显示小 chip，正文不重复文件名太长）。 */
  attachment?: { name: string; isImage: boolean }
  /** 助手消息定稿时固化的证据齐套度（流中长在页面上，答完留在回答顶部可回看）。 */
  readiness?: ReadinessState
  /**
   * 2026-10-28: 这一轮流式没拿到服务端终稿（断流/坏帧/服务端初步失败）。
   * 正文可能只是半截，气泡上必须带可见标记 —— 不许把半截当成品。
   */
  incomplete?: boolean
}

/**
 * 流式增量: 只动**最后一条**助手气泡（没有就新建），历史消息不动。
 * 抽成导出纯函数是为了让单测验证"终稿/增量落到哪条消息", 不必渲染整页。
 */
export function upsertStreamDraft(msgs: Msg[], content: string): Msg[] {
  const last = msgs[msgs.length - 1]
  if (last?.role === 'assistant') return [...msgs.slice(0, -1), { ...last, content }]
  return [...msgs, { role: 'assistant', content }]
}

/**
 * 终稿落地: 用服务端 `done.content`（落库全文，含对账附言/拒答）覆盖草稿。
 * - 已有助手气泡 → 原地替换，丢掉流式草稿；
 * - 只有终稿（拒答那条路一个 delta 都没发）→ 补一条气泡；
 * - 终稿和草稿都空 → 一条都不铺（空 ≠ 0/0，别造假空气泡）。
 */
export function settleStreamBubble(
  msgs: Msg[], content: string, incomplete = false, runId?: string | null,
  evidenceAudit?: EvidenceRowAudit | null,
): Msg[] {
  const last = msgs[msgs.length - 1]
  const text = content || (last?.role === 'assistant' ? last.content : '')
  if (!text) return msgs
  const flag = incomplete ? true : undefined
  const link = runId !== undefined ? { runId, evidenceAudit: evidenceAudit ?? null } : {}
  if (last?.role === 'assistant') {
    return [...msgs.slice(0, -1), { ...last, content: text, incomplete: flag, ...link }]
  }
  return [...msgs, { role: 'assistant', content: text, incomplete: flag, ...link }]
}

/** 提问归一化：与 `lib/copilot/match-run.ts` 同口径（空白折叠、去掉发送时带的 @ 前缀）。 */
function normalizeQuestion(text: string): string {
  return (text || '')
    .replace(/\s+/g, ' ')
    .replace(/^(@\S+\s+)+/, '')
    .trim()
}

/** 这条回答对应的问题是它前面最近的一条用户消息（页面内存的就是这一轮原文）。 */
function questionOfMessage(msgs: Msg[], index: number): string {
  if (msgs[index]?.role !== 'assistant') return ''
  for (let k = index - 1; k >= 0; k -= 1) {
    if (msgs[k].role === 'user') return normalizeQuestion(msgs[k].content)
  }
  return ''
}

/**
 * 同一个问题是否同一句：先比归一化全文，再退 24 字头（防一端被截断/多了标点）。
 * 与 `match-run.ts` 同一口径 —— 这里只是把它变成「按第几次出现」的可定位版本。
 */
function sameQuestion(a: string, b: string): boolean {
  if (!a || !b) return false
  if (a === b) return true
  const head = a.slice(0, 24)
  if (head.length < 8) return false
  return b.startsWith(head) || a.startsWith(b.slice(0, 24))
}

/**
 * 把一条回答对到它的那一轮 run（2026-10-29 P1 接线）。
 *
 * 与 `lib/copilot/match-run.ts` 的区别，也是页面能用的前提：
 *  - **同一会话里同一问题被问过多次**时，`runs.find` 永远命中最新那一轮（后端按 id desc 返回），
 *    旧回答会被挂上新 run —— 这是错轮。这里按「同问的第几次出现」对齐：最新一条回答对
 *    runs[0]，往前依次 runs[1]、runs[2]…；候选不够就返回 null，绝不复用别人的 run。
 *  - **切会话**时 `useCopilotRuns` 在新请求回来前还留着旧会话的列表，而提问文本又可能相同。
 *    因此候选 run 的 `conversation_id` 必须**明确等于**当前会话；为 null 或他会话一律不挂。
 *
 * 新消息以服务端持久化的 run_id 精确关联；旧消息没有该字段时仍为文本推断，
 * 推断必须标未验证。run 不在本会话时绝不回退匹配。
 */
export function resolveRunForMessage(
  msgs: Msg[],
  runs: CopilotRunRow[],
  index: number,
  conversationId: number | null,
): CopilotRunRow | null {
  // 没有会话就没有可归属的 run（新会话未落库时也不挂）
  if (conversationId == null) return null
  if (!Array.isArray(runs) || runs.length === 0) return null
  const storedRunId = msgs[index]?.role === 'assistant' ? msgs[index].runId : null
  if (storedRunId) return runs.find((r) => r.run_id === storedRunId && r.conversation_id === conversationId) ?? null
  const question = questionOfMessage(msgs, index)
  if (!question) return null

  const candidates = runs.filter(
    (r) => r.conversation_id === conversationId && sameQuestion(question, normalizeQuestion(r.question || '')),
  )
  if (!candidates.length) return null

  // 本条之后还有多少次同问：0 = 最新一条回答 → 对 runs[0]（后端按 id desc）
  let rank = 0
  for (let k = index + 1; k < msgs.length; k += 1) {
    if (msgs[k]?.role !== 'assistant') continue
    if (sameQuestion(questionOfMessage(msgs, k), question)) rank += 1
  }
  // 候选不够（某一轮没落库/被清理）→ null，绝不复用别人的 run
  return candidates[rank] ?? null
}

/**
 * 一条回答的溯源（页面唯一入口）。
 *
 * 纪律：
 *  - 草稿（`live`）不挂：终稿没到，回答与证据都还没定稿；
 *  - 关联优先服务端保存的 run_id；历史消息只能文本推断，未验证；
 *  - steps 只取「候选 run 自己的」详情（`details[run_id]`），没加载就是 null，
 *    不拿 `run.tool_calls` 冒充；
 *  - readiness 只收**这条消息定稿时固化的**齐套度：页面上正在跑的 stage 流属于当前草稿，
 *    挂到历史回答上就是把这一轮的取数结果说成上一轮的。
 */
export function buildMessageAnswerTrace(input: {
  msgs: Msg[]
  runs: CopilotRunRow[]
  details: Record<string, CopilotRunDetail>
  index: number
  conversationId: number | null
  live: boolean
  /** 消息自己的固化齐套度；传当前实时态就是错 */
  readiness?: ReadinessState | null
}): AnswerTrace | null {
  if (input.live) return null
  const run = resolveRunForMessage(input.msgs, input.runs, input.index, input.conversationId)
  const steps = run ? input.details[run.run_id]?.steps ?? null : null
  return buildAnswerTrace({
    run,
    steps,
    // 仅服务端持久化的 run_id 可标为已验证；历史文本匹配仍为推断。
    association: run && input.msgs[input.index]?.runId === run.run_id ? 'run-id' : 'question-text',
    readiness: input.readiness ?? null,
  })
}

/** 6 位 A 股代码（含北交所 4/8 开头）才允许查参谋卡，避免把日期/金额当代码。 */
const SYMBOL_RE = /^[03468]\d{5}$/

/**
 * 一个会话的图表记账（2026-10-29 三审）。
 *
 * `chart` 三态不能合并：`unknown`（历史会话）**既不同于** `chart`（确知带着快照），
 * **也不同于** `clean`（确知没带）。把它当 `clean` 就会重复"旧图每轮注入而界面装作没有"；
 * 把它当 `chart` 又会在重开历史会话时谎称"本页知道那段快照是什么"。
 */
type ConvChartState = 'chart' | 'clean' | 'unknown'
interface ConvRecord {
  /** 会话绑定标的（服务端 `stock_symbol`；不知道就是空串） */
  symbol: string
  chart: ConvChartState
  /** 本页亲手送进过的那段快照（只有 `chart` 态才有） */
  ctx?: ChartContext
  /**
   * 这个会话是在哪个账号下打开/创建的（2026-10-29 三审）。
   *
   * 同一标签页换号后，`chartCtx` 会被清掉 —— 但"这个会话属于上一个账号"这件事不能跟着丢：
   * 新账号若接着在同一只票上提问，光看 `chartCtx` 已经查不出串号，需要这份账号归属来兜底。
   */
  owner: string
}

/**
 * `/ai-copilot` 一级工作台（设计稿 §8.8，2026-10-18）。
 *
 * 为什么是一个独立一级页而不是把 ChatWidget 放大：
 *  - 侧栏「战术指令池」是**入口级的引导**，塞进右下角浮窗里没人看得见；
 *  - 这里可以同时放「对话流」和「参谋卡」：先给结论（卡），再让人追问（对话），
 *    符合"副驾驶先说结论"的产品直觉。
 *
 * 复用而非重写：SSE 走 `chatApi.sendMessageStream`（已含 stage/delta/done/error 与 signal），
 * 不在这里解析流；渲染走 SafeMarkdown（统一 sanitize 策略）。
 */
export default function AiCopilotPage({ presentation = 'page', visible = true, onBusyChange, entry, entrySearch }: {
  presentation?: 'page' | 'mini'; visible?: boolean; onBusyChange?: (busy: boolean) => void; entry?: AssistantEntry | null; entrySearch?: string
} = {}) {
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const inputRef = useRef<HTMLTextAreaElement>(null)
  useLayoutEffect(() => {
    const editor = inputRef.current
    if (!editor || !visible) return
    const resize = () => {
      const minimum = window.innerWidth < 768 ? 44 : 80
      const limit = Math.max(minimum, Math.min(160, Math.floor((window.visualViewport?.height ?? window.innerHeight) * 0.3)))
      editor.style.height = 'auto'
      const contentHeight = editor.scrollHeight + 2
      editor.style.height = `${Math.min(limit, Math.max(minimum, contentHeight))}px`
      editor.style.overflowY = contentHeight > limit ? 'auto' : 'hidden'
    }
    resize()
    window.addEventListener('resize', resize)
    window.visualViewport?.addEventListener('resize', resize)
    return () => {
      window.removeEventListener('resize', resize)
      window.visualViewport?.removeEventListener('resize', resize)
    }
  }, [input, visible, presentation])
  const [stage, setStage] = useState('')
  /**
   * 证据齐套度（2026-10-23）：把 stage 流折成按维度的 ✓/⏳/未取到。
   * ref 与 state 双份 —— 流结束的 finally 里要拿**最新值**固化到消息上，state 那一刻还没刷新。
   */
  const [readiness, setReadiness] = useState<ReadinessState>(emptyReadiness)
  const readinessRef = useRef<ReadinessState>(emptyReadiness())
  /** 回答里点中的价位锚（K 线画线由别的模块拥有，这里只做可见回执）。 */
  const [priceAnchor, setPriceAnchor] = useState<{
    value: number
    kind: PriceAnchorKind
    context: string
    symbol?: string
  } | null>(null)
  const [streaming, setStreaming] = useState(false)
  const [backgroundPending, setBackgroundPending] = useState(false)
  const consumedEntry = useRef<number | null>(null)
  const initialContext = useRef<AssistantEntry | null>(null)
  // 左栏（对话列表）可折叠：默认展开，状态持久化
  const [railOpen, setRailOpen] = useState<boolean>(() =>
    readBoolPref(typeof window === 'undefined' ? null : window.localStorage, RAIL_KEY),
  )
  const viewportPanel = useViewportPanel(presentation === 'page')
  const [mobileConversationsOpen, setMobileConversationsOpen] = useState(false)
  // 右面板折叠 = panelTab 为空(它本来就是可开可关的滑出面板);
  // 用 lastPanelTabRef 记住上次那个页签, 快捷键才能"关掉再打开回原来那页"
  const lastPanelTabRef = useRef<PanelTab>('runs')

  // 2026-10-25: 正在跟踪的后台 run（实时面板用 SSE 订阅它的工具流）
  const [liveRunId, setLiveRunId] = useState<string | null>(null)
  // 2026-10-25: 后台执行开关(长问题用: 提交即返回, 可离开页面)
  const [bgMode, setBgMode] = useState(false)
  const [convId, setConvId] = useState<number | null>(null)
  // 历史对话（2026-10-20）：后端的 conversations 接口早就有了，但面板一直没有入口，
  // 刷新即丢上下文。这里补抽屉：列出/打开/删除历史，并把 conversation_id 续上。
  // 2026-10-22 工作台对齐 Codex/Hermes：右侧面板（过程 / 记忆 / 定时 / 提案）+ 会话管理。
  // 过程=每轮工具时间线（治理页是全局视角，这里只看当前会话）；
  // 提案=参谋缺的分析维度（配方没覆盖的）→ 人裁决，采纳不等于生效（见 ProposalPanel 头注）。
  const [panelTab, setPanelTab] = useState<'runs' | 'memory' | 'schedule' | 'proposals' | null>(null)
  const [convQuery, setConvQuery] = useState('')
  const convMgr = useConversations()
  // 2026-10-25: 内联步骤条要用 runs，所以不再只在 panelTab==='runs' 时拉取。
  // 代价是打开会话多一个轻请求（limit 20），换来「答案下面直接能看到调过什么」。
  const runsState = useCopilotRuns({ conversationId: convId, enabled: visible })
  const [convs, setConvs] = useState<{ id: number; title: string; created_at: string }[]>([])
  const [error, setError] = useState<string | null>(null)
  /** 2026-10-28: 坏帧的可见提示（非致命：终稿到达就按终稿显示，别当没发生）。 */
  const [streamNotice, setStreamNotice] = useState<string | null>(null)
  /** 上下文锚点（?symbol= 带进来即显示），不触发参谋卡请求 */
  const [anchorSymbol, setAnchorSymbol] = useState('')
  /**
   * 图表上下文（P2, 2026-10-29）：从个股工作台带过来的**那一张图**的读数
   * （类型/市场/周期/可视区间/已确认价位）。
   *
   * 只在「从工作台进参谋」时读取一次（`readChartContext` 按 账号+代码+市场 精确匹配，
   * 同号多类型直接返回 null 不猜），之后不轮询、不自动取数 —— 用户屏幕上那张图是什么，
   * 带过来的就是什么；图变了要重新从工作台进入。
   *
   * 失效点（任一发生即清空，绝不跟着串）：用户清除、锚点换标的、切历史会话。
   */
  const [chartCtx, setChartCtx] = useState<ChartContext | null>(null)
  /**
   * 已经被服务端会话收下的图表快照（P2 修正, 2026-10-29）。
   *
   * 后端事实：`initial_context` 只在 `POST /chat/conversations` 时落库，
   * `_build_ai_messages()` 每一轮都把它拼进 system prompt，且没有接口能改/清它。
   * 所以「哪段快照进了哪个会话」必须在前端记牢：会话 21 收过 600111 的图，
   * 之后的追问要么继续承认这一点（界面显示 + 明说不撤回），要么换一个不带它的新会话，
   * 绝不能假装点一下清除就没了。
   */
  const [sentChart, setSentChart] = useState<{ conversationId: number; symbol: string } | null>(null)
  /** 图表上下文的可见提示（清除/换标的后的真实后果，不静默、不含糊）。 */
  const [chartNotice, setChartNotice] = useState<string | null>(null)
  /**
   * 当前打开会话的**绑定标的**（服务端 `stock_symbol`；本页新建时就是请求里那个）。
   * 空串 = 这个会话没有绑定标的（历史会话常见），此时任何"同一只票"的判断都不成立。
   */
  const [convBoundSymbol, setConvBoundSymbol] = useState('')
  /**
   * 正在拉某个历史会话的详情（`GET /chat/conversations/{id}` 在飞）。
   *
   * 在飞期间必须挡住发送：此刻 `convId` 还是**上一个**会话，而用户点开的那个会话
   * 到底带没带图表快照、绑定的是哪只票都还不知道 —— 发出去就是猜（2026-10-29 三审）。
   */
  const [convLoading, setConvLoading] = useState(false)
  /**
   * 当前打开的是"不是本页创建"的历史会话：它的 `initial_context` 服务端不回传，
   * 无法核验有没有带过图表快照，而后端仍会每轮重放。这条提示必须一直在（不随发问消失）。
   */
  const [historyCtxUnknown, setHistoryCtxUnknown] = useState(false)
  /**
   * 过期是"时间到了"的事，不是交互触发的事：只有图表上下文在场时每 30s 走一次时钟，
   * 条上才能及时把"进入时快照"标成"已过期、不发送"。
   */
  const [chartClock, setChartClock] = useState(() => Date.now())
  useEffect(() => {
    if (!chartCtx) return
    const timer = window.setInterval(() => setChartClock(Date.now()), 30_000)
    return () => window.clearInterval(timer)
  }, [chartCtx])
  /** 参谋卡只在用户主动点「看卡」时请求（brief 接口较重，常 10s+） */
  const [briefSymbol, setBriefSymbol] = useState('')
  // ── @ 提及（股票 / 内置智能体）──
  const [mentionHit, setMentionHit] = useState<MentionHit | null>(null)
  const [mentionTab, setMentionTab] = useState<MentionTab>('stock')
  const [mentionIdx, setMentionIdx] = useState(0)
  const [mentionLoading, setMentionLoading] = useState(false)
  const [mentionPool, setMentionPool] = useState<{ stocks: MentionStock[]; agents: MentionAgent[] }>({
    stocks: [],
    agents: [],
  })
  const [mentions, setMentions] = useState<(MentionStock | MentionAgent)[]>([])
  /** 中文输入法组合期间不触发 @ 面板（否则打拼音会边打边跳、回车误选） */
  const composingRef = useRef(false)
  const [attach, setAttach] = useState<PendingAttachment | null>(null)
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  /**
   * 在飞的所有流控制器（2026-10-28）。切会话**不中止**旧流（后端继续跑完落库，历史里还在），
   * 但卸载要一起断 —— 没人看了就别继续计费。所以这里存集合而不是单个 ref：
   * 两轮流并存时（切走旧流还没收、新会话又发了新流）只 abort 最后一个会漏掉前面的。
   */
  const liveStreams = useRef<Set<AbortController>>(new Set())
  /**
   * 会话 id → 这个会话的图表记账（`{ symbol, chart }`，2026-10-29 三审）。
   *
   * 只记 `sentChart`（当前会话）不够：用户点「新对话」再把自己刚才那个会话点回来时，
   * `openConversation` 会清空当前会话态，而服务端仍每轮重放那份 `initial_context`。
   * 更糟的是刷新页面后本页什么都不记得，换票检查完全失效（旧图快照被继续注入）。
   *
   * 所以按会话 id 记账，并且**区分三种状态**：
   *  - `chart`   本页亲手送进过图表快照（每轮重放，撤不回）；
   *  - `clean`   本页确知它没带快照（例如后台新建的那一轮）；
   *  - `unknown` 不是本页创建的会话，服务端不回传 `initial_context`，无法核验。
   * `unknown` 按"可能有"处理 —— 保守，但不许装成"没有"。
   */
  const convRecords = useRef<Map<number, ConvRecord>>(new Map())
  /** `openConversation` 的请求序号：只允许**最后一次**打开写入视图（避免慢 GET 覆盖用户刚点的会话）。 */
  const openSeqRef = useRef(0)
  /** `convId` 的最新值（异步回调里读，state 闭包会过期）。 */
  const convIdRef = useRef<number | null>(null)
  /** `chartCtx` 的最新值（窗口 focus/跨标签 storage 事件里读）。 */
  const chartCtxRef = useRef<ChartContext | null>(null)
  const mounted = useRef(true)
  /**
   * 2026-10-28 流式归属令牌: 切会话/新会话时 +1。
   * 旧流的 onDelta/onDone 回来时对不上令牌就整条丢弃 —— 否则上一轮的正文
   * 会写进用户刚切过去的那个会话（回归用例 tests/components/copilot-stream-final.test.tsx）。
   */
  const streamSeqRef = useRef(0)
  const scroller = useRef<HTMLDivElement>(null)
  useEffect(() => {
    mounted.current = true
    const live = liveStreams.current
    return () => {
      mounted.current = false
      // 卸载断掉所有在飞的流（含被切会话作废、但后端还在跑的那些）
      for (const ac of live) ac.abort()
      live.clear()
    }
  }, [])
  /**
   * 视图换主人（2026-10-28）：切会话 / 新会话 / 换入口上下文时调用。
   *
   * 令牌 +1 让旧流失去写入权；并把**它占着的本轮态**当场收走：
   * `streaming`（loading）/ `stage` / 证据齐套度 / 坏帧提示都属于「正在看的那一轮」。
   * 不能在旧流的 finally 里做这件事 —— 那时视图可能已经是别人的了，等于替别人清场
   * （回归用例 tests/components/copilot-stream-race.test.tsx）。
   */
  const detachStream = useCallback(() => {
    streamSeqRef.current += 1
    readinessRef.current = emptyReadiness()
    setReadiness(emptyReadiness())
    setStreaming(false)
    setStage('')
    setStreamNotice(null)
  }, [])
  useEffect(() => { onBusyChange?.(streaming || uploading || backgroundPending) }, [streaming, uploading, backgroundPending, onBusyChange])
  useEffect(() => { convIdRef.current = convId }, [convId])
  useEffect(() => { chartCtxRef.current = chartCtx }, [chartCtx])
  useEffect(() => {
    if (!entry || entry.id === consumedEntry.current || streaming || backgroundPending) return
    consumedEntry.current = entry.id
    initialContext.current = entry
    detachStream() // 2026-10-28: 换入口上下文 = 换会话视图, 旧流连带本轮态一起作废
    openSeqRef.current += 1 // 在飞的历史会话 GET 一并作废（别把视图拉回刚离开的会话）
    setConvLoading(false)
    setConvId(null)
    setConvBoundSymbol(entry.symbol)
    setMsgs([])
    setError(null)
    setAnchorSymbol(entry.symbol)
    // 换入口 = 新会话视图：上一段「已送给某个会话」的记账跟着作废
    setSentChart(null)
    setChartNotice(null)
    setHistoryCtxUnknown(false)
    /**
     * P2(2026-10-29): 从个股工作台进来时，把「用户刚才盯的那张图」一起带上。
     * 读的是工作台**真的发布过**的视图快照（`lib/copilot/chart-context`），
     * 拿不到/同号歧义/别人的账号 → null；这里不猜、不请求。
     *
     * 读侧还必须说清"为什么没有"：过期 / 同号歧义 / 已被用户清除都不能静默 ——
     * 静默会让用户以为"带上了"，或者以为"工作台没这个功能"。
     */
    const read = readChartContextDetailed(entry.symbol, entry.market ?? null)
    setChartCtx(read.ctx)
    if (read.expired) {
      setChartNotice('工作台的图表快照已超过 5 分钟，本次未带入（刷新页面后从工作台重新进入可重新抓取）。')
    } else if (read.ambiguous) {
      setChartNotice('同号有多张图（类型/市场/周期不唯一），为避免猜错，本次未带入图表上下文。')
    } else if (read.dismissed) {
      setChartNotice('你之前清除了这只票的图表上下文，本次未带入；刷新页面后从工作台重新进入可再次带上。')
    }
  }, [entry, streaming, backgroundPending, detachStream])

  /**
   * 锚点换标的 → 工作台那张图的上下文立刻失效（P2 隔离）。
   *
   * 用**派生值**而不是 effect 清 state：用户改问 000776 时，600111 的区间/价位一个都不许跟着走。
   * （effect 里比较 `anchorSymbol` 会读到上一轮的闭包值 —— 首帧锚点还是空串，刚带进来的上下文
   *   会被自己当场清掉；派生值没有这个时序坑，渲染/发送两处也共用同一个判断。）
   */
  /**
   * 发送前的 owner 复核也走进这个派生值（2026-10-29 复审）：同一标签页换号后，
   * A 的快照还在内存里，但此刻的提问属于 B —— 不能显示、更不能送。
   */
  const chartOwnerOk = chartCtx === null || chartContextOwnerMatches(chartCtx)
  const activeChartCtx = chartCtx && chartOwnerOk && chartCtx.symbol === anchorSymbol ? chartCtx : null
  /**
   * 这段快照是否已经随**当前这个会话**送达后端（会话级 `initial_context`，撤不回来）。
   * 只要为真，后端每轮追问都会继续读到它 —— 界面必须一直显示并如实标注。
   */
  const chartCommittedHere = sentChart !== null && convId !== null && sentChart.conversationId === convId
  /**
   * 顶部那条到底显示哪段上下文：
   *  - 还没送出：只在锚点还匹配时显示（用户改问别的票，它就不再指代当前问题）；
   *  - 已送进本会话：只要这个会话还在就显示（哪怕锚点被清/改）——
   *    否则用户看到"条没了"，模型却还在收旧区间，就是撒谎。
   */
  const chipCtx = chartCommittedHere && chartCtx && chartOwnerOk ? chartCtx : activeChartCtx
  /** 过期读数：可以显示"曾经是什么"，但绝不能送进模型，也不能再说它是当前视图。 */
  const chartCtxExpired = chartCtx !== null && !isChartContextFresh(chartCtx, chartClock)

  /**
   * 上下文带入（P0）：从个股工作台/外部链接进来时带 `?symbol=600111`，
   * 页面直接进入「正在看」状态，用户不必再输一次代码；带 `?q=` 时预填问题。
   * 用 URLSearchParams 而非 router hook：本页单测裸渲染不带 Router。
   */
  useEffect(() => {
    if (typeof window === 'undefined') return
    const sp = new URLSearchParams(entrySearch ?? window.location.search)
    const sym = (sp.get('symbol') || '').trim()
    if (SYMBOL_RE.test(sym)) setAnchorSymbol(sym)
    const q = (sp.get('q') || '').trim()
    if (q) setInput(q)
    // 入口扶正 2026-10-23: ?agent= 预填 @ 参谋 mention chip(只预填不自动发送, 等用户点发送);
    // ?date= 为所续分析的基准日(后续推演上下文预留, 本页当前无副作用)
    const agentParam = (sp.get('agent') || '').trim()
    if (agentParam) {
      const chip: MentionAgent = {
        type: 'agent',
        id: agentParam,
        label: agentParam === 'premarket_strategist' ? '盘前策略官' : agentParam,
        icon: '',
        desc: '',
        when: [],
        tool_count: 0,
      }
      setMentions((m) => (m.some((x) => mentionKey(x) === mentionKey(chip)) ? m : [...m, chip]))
    }
    void sp.get('date')
  }, [entrySearch])

  useEffect(() => {
    scroller.current?.scrollTo({ top: msgs.length || stage ? scroller.current.scrollHeight : 0 })
  }, [msgs, stage, presentation])

  /**
   * 选择附件：图片走多模态（`image_data` 直传模型），PDF/Excel/txt 走服务端解析文本。
   * 复用 ChatWidget 同一条 `/api/chat/upload`，不新造通道。
   */
  const handleFile = useCallback(
    async (file: File | null | undefined) => {
      if (!file) return
      if (file.size > MAX_ATTACHMENT_SIZE) {
        setError(ATTACH_TOO_LARGE)
        return
      }
      setError(null)
      setUploading(true)
      try {
        const res = await chatApi.uploadAttachment(file)
        setAttach({
          name: res.filename || file.name,
          text: (res.text || '').trim(),
          imageData: res.image_data,
          isImage: file.type.startsWith('image/'),
        })
        // 图片走多模态：OCR 失败不影响读图，只有「无图且无文本」才是真失败
        if (res.error && !res.image_data) setError(`${ATTACH_FAILED}: ${res.error}`)
      } catch (e) {
        setError(`${ATTACH_FAILED}: ${e instanceof Error ? e.message : '未知错误'}`)
      } finally {
        setUploading(false)
      }
    },
    [],
  )

  // @ 面板数据源：只在面板打开时请求，按检索词防抖
  useEffect(() => {
    if (!mentionHit) return
    let alive = true
    const timer = window.setTimeout(() => {
      setMentionLoading(true)
      getCopilotMentions(mentionHit.query)
        .then((res) => {
          if (!alive) return
          setMentionPool({ stocks: res?.stocks || [], agents: res?.agents || [] })
          setMentionIdx(0)
        })
        .catch(() => {
          if (alive) setMentionPool({ stocks: [], agents: [] })
        })
        .finally(() => alive && setMentionLoading(false))
    }, 150)
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [mentionHit])

  const mentionRows = mentionHit
    ? mentionTab === 'stock'
      ? filterMentions(mentionPool.stocks, mentionHit.query)
      : filterMentions(mentionPool.agents, mentionHit.query)
    : []

  const pickMention = (item: MentionStock | MentionAgent) => {
    if (!mentionHit) return
    const label = item.type === 'stock' ? item.name || item.symbol : item.label
    const next = applyMention(input, mentionHit, label)
    setInput(next.text)
    setMentions((m) => (m.some((x) => mentionKey(x) === mentionKey(item)) ? m : [...m, item]))
    setMentionHit(null)
  }


  /** 拉最近会话（左栏对话列表的数据源；抽屉取消后仍需在挂载时拉一次）。 */
  const loadHistory = useCallback(async () => {
    try {
      const rows = await chatApi.listConversations(60)
      if (!mounted.current) return
      setConvs((rows || []).map((c) => ({ id: c.id, title: c.title || '未命名对话', created_at: c.created_at })))
    } catch {
      setConvs([])
    }
  }, [])

  useEffect(() => {
    void loadHistory()
  }, [loadHistory])

  const openConversation = useCallback(async (id: number) => {
    /**
     * 2026-10-29 三审：GET 返回之前**一个字段都不许改**。
     *
     * 早先这里先 `setChartCtx(null)` + `setSentChart(null)` 再 `await`：
     *  - GET 失败 → 证据已经没了，而 `convId` 还是旧的，用户接着换票提问就被送进
     *    那个「创建时带着旧图快照」的会话（旧区间照旧每轮注入）；
     *  - GET 还在飞 → 同一个窗口，`sentChart` 已被同步清空，换票守卫同样失效。
     * 现在只在拿到**完整**详情后一次性替换视图；失败保持原样并明说没有切换。
     */
    const seq = ++openSeqRef.current
    setConvLoading(true)
    let detail: Awaited<ReturnType<typeof chatApi.getConversation>> | null = null
    try {
      detail = await chatApi.getConversation(id)
    } catch (e) {
      if (openSeqRef.current !== seq || !mounted.current) return
      setConvLoading(false)
      setError(
        `打开历史对话失败：${e instanceof Error ? e.message : '未知错误'}` +
          '（当前会话与图表上下文保持原样，没有切换）',
      )
      return
    }
    // 被更晚的一次打开取代：整条丢弃，绝不写半份（慢请求不许覆盖用户刚点的会话）
    if (openSeqRef.current !== seq || !mounted.current) return
    const rows = Array.isArray(detail?.messages) ? detail.messages : null
    if (!rows) {
      setConvLoading(false)
      setError('打开历史对话失败：服务端返回的会话内容不完整（缺少消息列表），已保持当前会话不变。')
      return
    }
    const meta = detail?.conversation && typeof detail.conversation === 'object' ? detail.conversation : null
    const sym = typeof meta?.stock_symbol === 'string' ? meta.stock_symbol.trim() : ''
    const known = convRecords.current.get(id) ?? null
    const chartState: ConvChartState = known?.chart ?? 'unknown'
    const ownCtx = chartState === 'chart' ? known?.ctx ?? null : null
    // 先记账再换视图：同一次批处理里，后续渲染读到的就是这份绑定
    convRecords.current.set(id, {
      symbol: sym || known?.symbol || '',
      chart: chartState,
      ctx: ownCtx ?? undefined,
      owner: currentAccountId() ?? '',
    })

    detachStream() // 2026-10-28: 切会话即作废旧流的写入权, 连它占着的 loading/stage/齐套度一起收
    setConvLoading(false)
    setConvId(id)
    setConvBoundSymbol(sym)
    // 绑定标的为空就如实清掉锚点：留着上一个会话的代码，等于替这个会话编了个标的
    setAnchorSymbol(sym)
    setMsgs(
      rows
        .filter((m) => m.role === 'user' || m.role === 'assistant')
        .map((m) => ({
          role: m.role as 'user' | 'assistant', content: m.content,
          runId: m.role === 'assistant' ? m.run_id : null,
          evidenceAudit: m.role === 'assistant' ? m.evidence_audit ?? null : null,
        })),
    )
    setError(null)
    setChartCtx(ownCtx)
    setSentChart(ownCtx ? { conversationId: id, symbol: ownCtx.symbol } : null)
    setChartNotice(
      ownCtx
        ? `这个会话创建时就带入了 ${ownCtx.symbol} 的图表快照（区间/价位），服务端每轮追问仍会注入、无法撤回。` +
            '换标的追问会新开一个不带它的会话；要彻底不带请点上方「清除并新开会话」。'
        : null,
    )
    /**
     * 历史会话的图表快照：`GET /chat/conversations/{id}` **不回传** `initial_context`，
     * 不是本页创建的会话无法核验它带没带过图。后端仍每轮重放那段文本，所以这里既不显示、
     * 也不声称"它没有/已清除"——用常驻提示如实说明不可核验，并写清后续行为（见渲染处）。
     */
    setHistoryCtxUnknown(chartState === 'unknown')
  }, [detachStream])

  const startNewConversation = useCallback(() => {
    detachStream() // 2026-10-28: 新会话 → 旧流不许再往这里写(本轮态一并收走)
    // 在飞的历史会话 GET 一并作废：不然它回来会把视图拉回用户刚离开的会话
    openSeqRef.current += 1
    setConvLoading(false)
    initialContext.current = null
    setConvId(null)
    setConvBoundSymbol('')
    setMsgs([])
    setError(null)
    // 新会话还没收到任何快照；上一会话的记账到此为止
    setSentChart(null)
    setChartNotice(null)
    setHistoryCtxUnknown(false)
  }, [detachStream])

  /**
   * 清除图表上下文（P2 修正, 2026-10-29）。
   *
   * 后端只在建会话那一刻收 `initial_context`，之后每轮无条件重放 —— 也就是说：
   *  - 还没发问：本地丢掉就真的没有了，服务端从来没收到；
   *  - 已经发问：那个会话里的快照**撤不回来**。要让后端真的不再用它，只能把后续追问
   *    引到一个不带它的新会话（旧会话留在列表里，不删、不打断正在跑的回答）。
   * 所以这里不会出现"点一下 = 已清除"的假象：每一种情况都给出对应后果。
   */
  const clearChartContext = useCallback(() => {
    const ctx = chipCtx
    if (!ctx) return
    if (streaming || backgroundPending) {
      // 「不确认就打断」是禁止项：本轮还在生成，先让它答完
      setChartNotice('本轮还在回答：等它结束后再清除（现在动手会打断这一轮）。')
      return
    }
    // 记成"该标的已清除"：工作台之后因缩放/重绘再次发布同一只票的快照也不会复活
    dismissChartContext(ctx)
    if (chartCommittedHere) {
      startNewConversation()
      setChartCtx(null)
      setChartNotice(
        '已新开一个不带图表上下文的会话（后续追问不会再带这张图）。上一会话未被删除，仍可在对话列表中找到 —— ' +
          '它创建时就已带入该快照，服务端无法撤回。',
      )
      return
    }
    setChartCtx(null)
    setChartNotice('图表上下文已从本地清除（尚未随提问送出，服务端没有收到过这段快照）。')
  }, [chipCtx, chartCommittedHere, streaming, backgroundPending, startNewConversation])

  /**
   * 删掉一个会话（2026-10-29 三审）。
   *
   * 删的就是当前打开的会话时，界面必须跟着清干净：否则 `convId` 指着一个已经不存在的会话，
   * 界面上还挂着"本会话已带入图表快照"，下一次提问要么发到 404，要么被后端重建出另一个
   * 带旧快照的会话。删除成功才清显示；失败照实说，不动视图。
   */
  const removeConversation = useCallback(
    async (id: number) => {
      const res = await convMgr.remove(id)
      if (!res?.ok) {
        setError(`删除对话失败：${res?.error || '未知原因'}`)
        return
      }
      const hadChart = convRecords.current.get(id)?.chart === 'chart'
      convRecords.current.delete(id)
      if (convIdRef.current !== id) return
      detachStream()
      openSeqRef.current += 1
      setConvLoading(false)
      initialContext.current = null
      setConvId(null)
      setConvBoundSymbol('')
      setMsgs([])
      setSentChart(null)
      setHistoryCtxUnknown(false)
      if (hadChart) setChartCtx(null)
      setChartNotice(`会话 #${id} 已删除，本页不再显示或引用它的图表快照。`)
    },
    [convMgr, detachStream],
  )

  /**
   * 切账号时把别人的图表快照从显示上撤掉（2026-10-29 三审）。
   *
   * 同一标签页 A 登出、B 登录后，A 的快照还留在内存里：之前只在发送前复核，
   * 于是 B 的界面上一直挂着 A 看的区间/价位，看着像自己的图。回到本页/跨标签存储变化时核一次，
   * 对不上就从显示上撤掉，并说清它不会被发送 —— 不宣称服务端数据被删除（客户端删不了）。
   */
  useEffect(() => {
    const checkAccount = () => {
      const ctx = chartCtxRef.current
      if (!ctx) return
      if (chartContextOwnerMatches(ctx, currentAccountId())) return
      setChartCtx(null)
      setSentChart(null)
      setChartNotice(
        '当前登录账号已切换：这张图的快照属于上一个账号，已从界面移除，也不会随本轮提问发送；' +
          '后续提问也不会沿用上一个账号的会话。',
      )
    }
    const onVisibility = () => {
      if (document.visibilityState === 'visible') checkAccount()
    }
    window.addEventListener('focus', checkAccount)
    window.addEventListener('storage', checkAccount)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      window.removeEventListener('focus', checkAccount)
      window.removeEventListener('storage', checkAccount)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [])



  /**
   * 后台执行（2026-10-25，P0-0.2 前端）。
   *
   * 为什么不是默认：逐字流式的体验用户已经在用；这条路解决的是**长问题**——
   * 一次深问 20~130s，关页面/断网/代理超时整轮白烧。开着它提交即返回，人可以先走。
   *
   * 诚实纪律：进度只说状态与步数（不编百分比）；失败把服务端 error 原文透出来；
   * 轮询有上限（10 分钟），超时不再假装还在跑。
   */
  const runInBackground = useCallback(
    async (question: string, attachment?: { name: string; isImage: boolean }, freshConversation = false, turnSymbol = '') => {
      setBackgroundPending(true)
      /**
       * 视图归属快照（2026-10-29 三审）：后台轮可能跑几分钟，期间用户完全可以去点开别的会话。
       * 早先完成时无条件 `openConversation(cid)`，等于把用户从正在看的会话里拽走 ——
       * 那不是"展示结果"，是抢视图。整轮开始时记下令牌，回来对不上就不动视图。
       */
      const seqAtSubmit = streamSeqRef.current
      setMsgs((m) => [...m, { role: 'user', content: question, attachment }])
      setInput('')
      setAttach(null)
      setMentions([])
      setMentionHit(null)
      setError(null)
      const placeholder = `⏳ ${progressText(null)}`
      setMsgs((m) => [...m, { role: 'assistant', content: placeholder }])
      // 归属感知（OCR error, 2026-10-30）：轮询每 2s 就 patch 一次，而 `msgs` 是**共享**
      // 的最后一条助手气泡。用户在 10 分钟轮询期间点开别的会话后，旧写法会把那个会话的
      // 最后一条回答覆盖成「⏳ 正在分析…」/「❌ 提交失败…」—— 纯前端内容错乱。
      // 令牌对不上（视图已切走/又发了新问题）就整条丢弃，服务端结果不受影响。
      const patch = (content: string) =>
        setMsgs((m) => {
          if (streamSeqRef.current !== seqAtSubmit) return m
          const last = m[m.length - 1]
          if (!last || last.role !== 'assistant') return m
          return [...m.slice(0, -1), { ...last, content }]
        })

      try {
        const submitted = await submitCopilotRun(question, freshConversation ? null : convId)
        if (!mounted.current) return
        setLiveRunId(submitted.run_id)
        const deadline = Date.now() + 10 * 60 * 1000
        let last = null as Awaited<ReturnType<typeof getCopilotRunStatus>> | null
        while (mounted.current && Date.now() < deadline) {
          await new Promise((r) => setTimeout(r, 2000))
          if (!mounted.current) return
          last = await getCopilotRunStatus(submitted.run_id)
          if (!mounted.current) return
          patch(`⏳ ${progressText(last)}`)
          if (isTerminalStatus(last.status)) {
            // 竞态（2026-10-25 生产实测）：轨迹先写终态，执行体随后才补 conversation_id，
            // 前端一看到终态就断开会读到 null。补拉几次，拿到会话 id 为止（最多 ~2s）。
            for (let k = 0; k < 4 && !last.conversation_id; k += 1) {
              await new Promise((r) => setTimeout(r, 500))
              if (!mounted.current) return
              last = await getCopilotRunStatus(submitted.run_id)
              if (!mounted.current) return
            }
            break
          }
        }
        if (!last || !isTerminalStatus(last.status)) {
          patch('⚠️ 后台任务超过 10 分钟仍未结束，已停止跟踪（可在「参谋任务」页查看最终状态）')
          return
        }
        if (last.status === 'ok' || last.status === 'degraded' || last.status === 'succeeded') {
          /**
           * 只在**同一会话**的后台轮才允许回落 `convId`（2026-10-29 复审）。
           * 换票/换号的后台轮是 `freshConversation`：服务端新建了会话，旧 `convId` 里
           * 还带着创建时的图表快照 —— 回落过去等于把用户拉回"旧区间照旧注入"的那个会话。
           * 拿不到新会话 id 就如实说拿不到，不动视图。
           */
          const cid = last.conversation_id ?? (freshConversation ? null : convId)
          if (cid) {
            /**
             * 换票/换号的后台轮 = 服务端新建了会话，而这条路径**不带** `initial_context`：
             * 本页确知它没有图表快照，先记账再打开，免得 `openConversation` 把
             * "历史会话不可核验"扣到一个本页刚建的干净会话头上（2026-10-29 三审）。
             */
            if (freshConversation) {
              convRecords.current.set(cid, {
                symbol: turnSymbol,
                chart: 'clean',
                owner: currentAccountId() ?? '',
              })
            }
            if (streamSeqRef.current === seqAtSubmit) {
              // 正文已由服务端落库，重新拉一次会话即可（不在前端拼答案）
              if (cid !== convId) setConvId(cid)
              await openConversation(cid)
              if (last.status === 'degraded') {
                setError('本轮部分工具失败，结论可能不完整（展开「过程」可看是哪一步）')
              }
            } else if (mounted.current) {
              /**
               * 用户已经切走：只记账，不把视图抢回来，也不往别人正在看的会话里写提示。
               * 结果在会话列表里，点开就能看（那里会用真实记账显示有没有快照）。
               */
              setChartNotice(`后台任务已完成，结果在会话 #${cid}（你没有停在这一轮，本页没有自动切过去，也未删除任何会话）。`)
            }
          } else {
            patch('✅ 已完成，但没拿到会话 id（新会话已建，可在对话列表中找到；本页没有把视图切回旧会话）')
          }
        } else {
          patch(`❌ ${progressText(last)}`)
        }
      } catch (e) {
        patch(`❌ 提交失败：${e instanceof Error ? e.message : String(e)}`)
      } finally {
        if (mounted.current) setBackgroundPending(false)
      }
    },
    [convId, openConversation],
  )

  const send = useCallback(
    async (text: string) => {
      const words = text.trim()
      // 只有附件没打字也允许发（截图直接问"这是什么"），所以附件在场时不要求文字
      // 2026-10-29 复审：后台任务在飞时同样不许再发 —— 那时 `convId` 仍指向旧会话，
      // 再发一条就会把新一轮塞进一个"创建时带着旧图快照"的会话里（用户看不到任何提示）。
      // 2026-10-29 三审：正在拉某个历史会话的详情时也不许发 —— `convId` 还是上一个会话，
      // 而用户刚点的那个会话带没带快照、绑定哪只票都还不知道，发出去就是猜。
      if ((!words && !attach) || streaming || uploading || backgroundPending || convLoading) return
      const attachPart = attach
        ? attach.text
          ? `[${ATTACH_CHIP_LABEL}: ${attach.name}]\n${attach.text}`
          : attach.imageData
            ? `[${ATTACH_CHIP_LABEL}: ${attach.name}]（图片，已由视觉模型转写）`
            : `[${ATTACH_CHIP_LABEL}: ${attach.name}]\n${ATTACH_EMPTY}`
        : ''
      const content = [words, attachPart].filter(Boolean).join('\n\n')
      const sentAttachment = attach ? { name: attach.name, isImage: attach.isImage } : undefined
      const imageData = attach?.imageData
      /**
       * 发送前再核一次账号（2026-10-29 复审 + 三审）：读 `chartCtx` 是进页面那一刻的事，换号后
       * A 的快照可能还在内存里。两道检查都要：
       *  - 快照 owner 对不上 → 不带快照；
       *  - **会话归属对不上**（这个会话是上一个账号打开的）→ 不复用那个会话。
       * 只清快照不够：同号追问会把 B 的问题送进 A 的会话里。
       */
      const ownerNow = currentAccountId() ?? ''
      const viewRec = convId !== null ? convRecords.current.get(convId) ?? null : null
      const ownerChanged =
        (chartCtx !== null && !chartContextOwnerMatches(chartCtx, ownerNow || null)) ||
        (viewRec !== null && viewRec.owner !== '' && ownerNow !== '' && viewRec.owner !== ownerNow)
      if (ownerChanged) {
        setChartCtx(null)
        setChartNotice('当前登录账号已切换：这张图的快照属于上一个账号，本轮不带入，也不复用上一个账号的会话。')
      }
      /**
       * 本轮提问指定了哪只票（2026-10-29 三审）：`@` 选的票 > 正文代码 > 页面锚点。
       * `explicit` 说明这个指向是不是用户自己写明的 —— 后台轮据此判断能不能复用旧会话。
       */
      const mentionedStock = mentions.find((m) => m.type === 'stock') as MentionStock | undefined
      const turnTarget = resolveTurnSymbol({ mentioned: mentionedStock?.symbol, text: words, anchor: anchorSymbol })
      // 后台模式：附件暂不支持（附件要先过视觉转写，走原流式路径）
      if (bgMode && !attach) {
        /**
         * 后台任务同样会把会话交给后端（`conversation_id`），旧会话的快照照样会被重放。
         *
         * 早先这里只从正文抠 6 位代码：`@` 选中另一只票、只说名字不说代码、以及会话绑定标的
         * 本身不确定（历史会话）这三种情况都判不出换票，旧图快照被继续注入。
         * 现在只有「这一轮自己写明了标的」且与会话绑定标的一致才复用；其余一律开新会话
         * （新会话不带 `initial_context`，旧会话保留、不删除、不打断）。
         */
        const rec = convId !== null ? convRecords.current.get(convId) ?? null : null
        const chartRisk = ownerChanged || (rec !== null ? rec.chart !== 'clean' : sentChart !== null)
        const trustedSame =
          turnTarget.explicit && Boolean(turnTarget.symbol) && rec !== null && turnTarget.symbol === rec.symbol
        const bgStale = chartRisk && !trustedSame
        if (bgStale) {
          /**
           * 旧会话**当场作废**（2026-10-29 复审）：只清 `sentChart` 不够 ——
           * 后台轮结束若拿不到新会话 id 会回落到 `convId`，界面上还会"回到"那个带快照的会话；
           * 期间用户再提问也会顺着 `convId` 送进旧会话。
           */
          setConvId(null)
          setConvBoundSymbol('')
          setSentChart(null)
          setChartCtx(null)
          if (chartCtx) unpublishChartContext(chartContextKey(chartCtx))
          setChartNotice(
            ownerChanged
              ? '账号已切换：这一轮在新会话里后台执行，不沿用上一个账号的快照与会话。'
              : !turnTarget.explicit || !turnTarget.symbol
                ? `后台这一轮没有写明标的（正文没写代码，也没 @ 选票），无法确认问的是不是 ${rec?.symbol || '上一张图的票'}；` +
                  '为避免沿用旧图的快照，已在没有图表快照的新会话里执行，上一会话保留在列表里。'
                : `标的已换（${rec?.symbol || '上一标的'} → ${turnTarget.symbol}）：这一轮在新会话里后台执行，不再沿用旧图的快照。`,
          )
        }
        await runInBackground(content, sentAttachment, bgStale, turnTarget.symbol)
        return
      }
      setError(null)
      setChartNotice(null)
      setMsgs((m) => [...m, { role: 'user', content, attachment: sentAttachment }])
      setInput('')
      setAttach(null)
      setMentions([])
      setMentionHit(null)
      setStreaming(true)
      setStage('正在准备上下文…')
      // 新一轮：齐套度清零（上一轮的痕已经固化在它自己的消息上了）
      readinessRef.current = emptyReadiness()
      setReadiness(readinessRef.current)
      setPriceAnchor(null)
      let assistant = ''
      // 2026-10-28: 本轮流的归属令牌。切会话/新会话来时 +1, 迟到的回调对不上就整条丢弃。
      const seq = ++streamSeqRef.current
      const isCurrent = () => mounted.current && streamSeqRef.current === seq
      const ac = new AbortController()
      liveStreams.current.add(ac)
      setStreamNotice(null)
      try {
        // 标的解析优先级：@ 指定 > 本条文字里的代码 > 页面锚点（正在看 XXX）
        const sym = turnTarget.symbol
        if (sym && sym !== anchorSymbol) setAnchorSymbol(sym)

        /**
         * 换标的 / 会话快照不可核验 + 当前会话（2026-10-29 三审合并判断）：
         *
         * 后端 `_build_ai_messages()` 每轮都会把该会话的 `initial_context` 拼进系统提示，
         * 而 `_bind_conv_symbol()` 换票时只改 `stock_symbol`、不动那段快照 —— 继续在旧会话里问
         * 000776，模型手里仍有一份 600111 的区间/价位。刷新不改写服务端，唯一诚实的做法是
         * 把这一轮放进**不带快照的新会话**（旧会话保留，不删除、不打断任何流）。
         *
         * 会话记账三态（见 `ConvRecord`）：
         *  - `chart`：本页送过快照 → 只有本轮标的与会话绑定标的一致才留着；
         *  - `unknown`：历史会话，服务端不回传它带没带过图 → 按"可能有"处理，
         *    标的对不上、或压根判不出本轮标的，都新开会话；
         *  - `clean`：确知没带 → 换票照旧留在原会话（后端换绑是正常行为，没有快照可串）。
         * 换号（`ownerChanged`）一律新会话。
         */
        const convRec = convId !== null ? convRecords.current.get(convId) ?? null : null
        const chartRisk = convRec !== null ? convRec.chart !== 'clean' : sentChart !== null
        const staleChartConv = ownerChanged || (chartRisk && (!sym || sym !== (convRec?.symbol ?? sentChart?.symbol ?? '')))
        if (staleChartConv) {
          // 新会话从这一条问话开始：屏幕上不再混着上一个会话的历史，避免"看着是同一个对话"
          setMsgs([{ role: 'user', content, attachment: sentAttachment }])
          setConvId(null)
          setConvBoundSymbol('')
          setSentChart(null)
          if (chartCtx) unpublishChartContext(chartContextKey(chartCtx))
          setChartCtx(null)
          setChartNotice(
            ownerChanged
              ? '账号已切换：为避免把上一个账号的图表快照与会话带给当前账号，这一轮已新开会话；上一会话未被删除。'
              : convRec?.chart === 'unknown'
                ? `这个会话不是本页创建的，服务端不回传它创建时是否带过图表快照；这一轮问的是 ${sym || '（无法判定）'}，` +
                  `与它的绑定标的（${convRec.symbol || '未绑定'}）对不上或无法确认 —— 已新开一个不带快照的会话；上一会话未被删除。`
                : !sym
                  ? '这一轮判断不出问的是哪只票，而本会话带着创建时的图表快照；已新开一个不带快照的会话，避免把旧区间当成新问题。'
                  : `标的已换（${convRec?.symbol || sentChart?.symbol} → ${sym}）：为避免沿用上一会话创建时的图表快照，` +
                    '这一轮已新开一个会话；上一会话未被删除，仍在对话列表里（它的快照不会跟着搬过来）。',
          )
        }
        let id = staleChartConv ? null : convId
        if (!id) {
          /**
           * P2(2026-10-29): 图表上下文只在这一轮标的与它一致时才算数 —— 隔离的最后一刀。
           * 不一致（用户改问别的票）就当没带过，绝不把 A 的区间/价位塞进 B 的会话。
           */
          const chartForTurn =
            activeChartCtx && sym && activeChartCtx.symbol === sym && isChartContextFresh(activeChartCtx)
              ? activeChartCtx
              : null
          if (activeChartCtx && activeChartCtx.symbol === sym && !chartForTurn) {
            // 过期读数：宁可这轮不带，也不把十分钟前的区间说成"你现在看的图"
            setChartNotice('图表快照已超过 5 分钟，本轮未带入（需要时请重新从工作台进入）。')
          }
          const pageSnapshot =
            initialContext.current?.symbol === sym && initialContext.current.pageContext
              ? initialContext.current.pageContext
              : `用户从【数智参谋】进入，当前标的 ${sym}。`
          const conv = await chatApi.createConversation(
            sym
              ? {
                  stock_symbol: sym,
                  stock_market: initialContext.current?.symbol === sym ? initialContext.current.market || marketOf(sym) : marketOf(sym),
                  /**
                   * 图表上下文与页面快照同路（会话级 `initial_context`，后端每轮都会附加到系统提示）。
                   * 语境里明写"进入时快照 / 用户提问才引用 / 不得自动下单与画线"，
                   * 由 `chartContextPrompt` 统一产出，避免两处措辞漂移。
                   */
                  initial_context: chartForTurn
                    ? `${pageSnapshot}\n\n${chartContextPrompt(chartForTurn)}`
                    : pageSnapshot,
                }
              : { initial_context: '用户从【数智参谋】一级页进入，尚未指定标的。' },
          )
          id = conv.id
          /**
           * 落库即记账（2026-10-29 三审）：会话一旦建好，`initial_context` 就永久留在服务端，
           * **与这一轮有没有发出去无关**。用户可能在建会话在飞时切走/点「新对话」，
           * 那一轮随之丢弃 —— 但会话（和它的图表快照）已经存在。不记这笔账，之后重开它
           * 只会看到一句泛化的「历史会话不可核验」，换票检查也一并失效。
           */
          convRecords.current.set(id, {
            symbol: sym || '',
            chart: chartForTurn ? 'chart' : 'clean',
            ctx: chartForTurn ?? undefined,
            owner: currentAccountId() ?? '',
          })
          // 建会话是异步的：等回来时用户可能已经切走/发了新一轮 —— 那时这条流的
          // 会话 id 不许把视图抢回去（否则"切到历史会话"会被一瞬间弹回旧会话）。
          if (!isCurrent()) {
            if (mounted.current) {
              // 用户此刻开着的正好是这个会话：承认它带着快照（虽然这一轮问题没发出）
              if (convIdRef.current === id) {
                setConvBoundSymbol(sym)
                setChartCtx(chartForTurn)
                setSentChart(chartForTurn ? { conversationId: id, symbol: chartForTurn.symbol } : null)
                setHistoryCtxUnknown(false)
              }
              setChartNotice(
                chartForTurn
                  ? `已为 ${sym} 建好会话 #${id} 并带入了图表快照，但这一轮问题没有发出（视图已切换）。` +
                      '该会话留在对话列表里，服务端会保留这段快照，本页不会自动复用它。'
                  : `已为 ${sym || '（未指定标的）'} 建好会话 #${id}，但这一轮问题没有发出（视图已切换）。该会话留在对话列表里。`,
              )
            }
            return
          }
          setConvId(id)
          setConvBoundSymbol(sym)
          setHistoryCtxUnknown(false)
          /**
           * 记账：这个会话从此刻起永久带着这段快照（后端每轮都会重放）。
           * 没带快照建会话时写 clean —— 免得旧会话的记账串到新会话上。
           */
          setSentChart(chartForTurn ? { conversationId: id, symbol: chartForTurn.symbol } : null)
        }
        const result = await chatApi.sendMessageStream(
          id,
          content,
          {
            onStage: (s) => {
              if (!isCurrent()) return
              setStage(s)
              // stage 里能认出工具（`正在取数 3/10：get_xxx`）才进齐套度；
              // 散文提示（「正在准备上下文…」）仍走下面那条单行 spinner，语义不混。
              const next = foldStageEvent(readinessRef.current, s)
              if (next !== readinessRef.current) {
                readinessRef.current = next
                setReadiness(next)
              }
            },
            onDelta: (d) => {
              if (!isCurrent()) return
              assistant += d
              setStage('')
              setMsgs((m) => upsertStreamDraft(m, assistant))
            },
            onDone: (msg) => {
              if (!isCurrent()) return
              // 2026-10-28: 终稿是落库全文 —— 对账附言、零工具拒答都只在这里,
              // 流式草稿必须被它替换(而不是像以前那样直接丢掉 onDone)。
              const text = typeof msg?.content === 'string' && msg.content ? msg.content : assistant
              assistant = text
              setStage('')
              setMsgs((m) => settleStreamBubble(
                m, text, false, typeof msg?.run_id === 'string' ? msg.run_id : null,
                msg?.evidence_audit ?? null,
              ))
            },
            onError: (e) => {
              if (!isCurrent()) return
              setError(e)
            },
            onMalformed: () => {
              // 坏帧的具体数量在返回值里（result.corruptedFrames），收尾统一提示；
              // 这里只保证"不静默"这条链是通的, 不打断用户已看到的正文。
            },
          },
          ac.signal,
          imageData,
          sym ? { symbol: sym, market: marketOf(sym) } : undefined,
          mentions.map((m) => ({
            type: m.type,
            value: m.type === 'stock' ? m.symbol : m.id,
            label: m.type === 'stock' ? m.name || m.symbol : m.label,
          })),
        )
        if (!isCurrent()) return
        // 2026-10-28: 没拿到终稿 = 失败, 不许把半截正文当成品亮着
        const streamResult = result as ChatStreamResult | undefined
        if (streamResult?.failure) {
          setError(streamResult.failure)
          setMsgs((m) => settleStreamBubble(m, streamResult.content || assistant, true))
        } else if (streamResult?.corruptedFrames) {
          setStreamNotice(`流式数据有 ${streamResult.corruptedFrames} 处损坏，已按服务端终稿校正`)
        }
      } catch (e) {
        // 2026-10-28: 旧流（已切走/已被新流顶掉）的失败文案只属于它自己那一轮,
        // 不许挂到别人正在看的会话上（回归用例 tests/components/copilot-stream-race.test.tsx）。
        if (!isCurrent()) return
        setError(e instanceof Error ? e.message : '对话失败')
        // 2026-10-28 验收（P0）：fetch 中途 reject 时屏幕上的半截正文**必须**带"不完整"标记。
        // 原来这里只 setError —— 用户看着几行正文正常亮着，会当成结论读（那几行甚至连终稿附言都没有）。
        // `assistant` 为空时 settleStreamBubble 一次都不铺（空 ≠ 半成品）。
        setMsgs((m) => settleStreamBubble(m, assistant, true))
      } finally {
        // 收尾同样要认主人：`streaming`/`stage`/齐套度/坏帧提示都是**当前**那一轮的显示态。
        // 早先这里无条件 setStreaming(false)+setStage('')，旧流一结束就把新流的 spinner 和
        // 取数阶段提示抹掉（用户看着还在跑的答案突然"消失"）；旧流该清的东西已在
        // detachStream（切会话/新会话那一刻）清过，这里再清就是替别人清场。
        liveStreams.current.delete(ac)
        if (isCurrent()) {
          setStreaming(false)
          setStage('')
          // 2026-10-25: 答完刷新 run 列表 —— 否则内联步骤条摘要用的是打开会话那一刻的快照
          // （实测新回答显示 "0 步 · -- · running"，因为 run 行在发问前就抓好了）
          runsState.reload()
          // 后端 run 行的最终状态可能比 SSE 收尾晚一拍，再补一次
          window.setTimeout(() => { if (mounted.current) runsState.reload() }, 1600)
          // 定稿：pending → ok（流结束了，取数阶段已经过去），然后把这条痕固化到助手消息上。
          // 没有助手消息（首字都没出来就失败）时不铺空条 —— 空 ≠ 0/0。
          const settled = settleReadiness(readinessRef.current)
          readinessRef.current = emptyReadiness()
          setReadiness(emptyReadiness())
          if (settled.items.length) {
            setMsgs((prev) => {
              const last = prev[prev.length - 1]
              if (!last || last.role !== 'assistant') return prev
              return [...prev.slice(0, -1), { ...last, readiness: settled }]
            })
          }
        }
      }
    },
    [attach, convId, anchorSymbol, mentions, streaming, uploading, backgroundPending, runsState, bgMode, runInBackground, activeChartCtx, chartCtx, sentChart, convLoading],
  )

  // 动作回写(P1): AI 只能提议, 用户点确认才落库 —— 融合的硬指标是"能改页面"。
  const [metrics, setMetrics] = useState<CopilotMetrics | null>(null)
  const [pending, setPending] = useState<PendingAction[]>([])
  /**
   * 2026-10-28 验收（P0）：待确认动作的**提交中**展示态 + 就地失败提示。
   *
   * 现场两个问题：连点「确认」发两次 POST（自选加两条）；确认失败只有 unhandled rejection，
   * 界面上一个字都没有，用户只能反复点。所以：
   *  - `acting` 只负责展示（禁用按钮/aria-busy），**重入守卫是 ref** ——
   *    state 要等下一次渲染才可见，同一个事件循环里的第二次点击会直接穿透；
   *  - 失败**不清行**（失败 = 没落库），把原因挂在动作旁边让人重试；
   *  - 只有成功才 `loadPending()` 刷新。
   */
  const [acting, setActing] = useState<{ id: number; kind: 'apply' | 'reject' } | null>(null)
  const [actError, setActError] = useState<{ id: number; message: string } | null>(null)
  const actLock = useRef(false)
  const loadPending = useCallback(() => {
    void getPendingActions()
      .then((rows) => {
        const list = Array.isArray(rows) ? rows : []
        setPending(list)
        // 行已经不在列表里 → 它的旧失败文案也没意义了（防止刷新回来后挂个过期错误）
        setActError((cur) => (cur && list.some((r) => r.id === cur.id) ? cur : null))
      })
      .catch(() => setPending([]))
  }, [])
  useEffect(() => {
    if (!visible) return
    loadPending()
    const timer = window.setInterval(loadPending, 20_000)
    return () => window.clearInterval(timer)
  }, [loadPending, visible])

  // 治理块（P3, 2026-10-20）：近 7 天运行数 / 成功率 / 平均工具数 / P95 耗时。
  useEffect(() => {
    if (!visible) return
    let cancelled = false
    const load = () => {
      void getCopilotMetrics(7)
        .then((m) => { if (!cancelled) setMetrics(m) })
        .catch(() => { if (!cancelled) setMetrics(null) })
    }
    load()
    const timer = window.setInterval(load, 60_000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [visible])

  const act = useCallback(
    async (id: number, kind: 'apply' | 'reject') => {
      // 同步守卫（ref 立即生效）：连点两次「确认」只发一次 POST
      if (actLock.current) return false
      actLock.current = true
      setActing({ id, kind })
      setActError(null)
      const target = pending.find((p) => p.id === id)
      const symbol = String((target?.payload as Record<string, unknown> | undefined)?.symbol ?? '')
      const verb = kind === 'apply' ? '确认' : '忽略'
      try {
        if (kind === 'apply') await applyPendingAction(id)
        else await rejectPendingAction(id)
        if (!mounted.current) return true
        loadPending() // 成功才刷新（后端不再返回这一条 → 行自然消失）
        return true
      } catch (e) {
        if (!mounted.current) return false
        // 失败 = 没落库：行原样留着，就地报错让人重试。
        // 绝不只抛给控制台 —— `void act(...)` 的 rejection 没人接，界面上等于什么都没发生。
        const reason = e instanceof Error && e.message ? `：${e.message}` : ''
        setActError({
          id,
          message: `${verb}失败${reason}。${symbol ? `${symbol} 的这条动作` : '这条动作'}没有生效，请重试`,
        })
        return false
      } finally {
        // 锁无条件释放（卸载后也必须放）；只有展示态受挂载状态约束
        actLock.current = false
        if (mounted.current) setActing(null)
      }
    },
    [loadPending, pending],
  )

  /**
   * 流式中的助手气泡是否已经落屏：决定了齐套度条挂在哪 ——
   * 气泡已出现就贴在气泡顶部（定稿时原地不动），还没首字就先挂在对话流尾部。
   */

  /**
   * 快捷键（2026-10-26）：Cmd/Ctrl+Enter 发送、Cmd/Ctrl+K 新会话、Cmd/Ctrl+\ 折叠右面板。
   * 判定逻辑在 lib/copilot/layout.ts（可单测），这里只做接线。
   */
  useEffect(() => {
    if (!visible) return
    const onKey = (e: KeyboardEvent) => {
      if (presentation === 'mini' && !viewportPanel.current?.contains(document.activeElement)) return
      const t = e.target as HTMLElement | null
      const tag = (t?.tagName ?? '').toLowerCase()
      const inEditable =
        tag === 'input' || tag === 'textarea' || Boolean(t?.isContentEditable)
      const action = resolveShortcut(
        { key: e.key, metaKey: e.metaKey, ctrlKey: e.ctrlKey, altKey: e.altKey, shiftKey: e.shiftKey,
          isComposing: (e as KeyboardEvent & { isComposing?: boolean }).isComposing },
        { inEditable, streaming },
      )
      if (!action) return
      if (action === 'send') {
        e.preventDefault()
        void send(input)
        return
      }
      if (action === 'new-conversation') {
        e.preventDefault()
        startNewConversation()
        return
      }
      if (action === 'toggle-panel') {
        e.preventDefault()
        setPanelTab((cur) => {
          if (cur) {
            lastPanelTabRef.current = cur
            return null
          }
          return lastPanelTabRef.current
        })
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [input, send, streaming, startNewConversation, setPanelTab, visible, presentation, viewportPanel])

  /** 空态"最近一次"回链用：最近一次运行（真实数据；没有就是 null，整块不渲染，不留死入口） */
  const lastRun = runsState.runs.length > 0 ? runsState.runs[0] : null

  /**
   * 回答 ↔ run：**只按提问文本匹配**，永不自称 run_id 关联（服务端没给这个关联）。
   * 同问多轮按出现次序对齐、切会话时拒收他会话的 run，见 `resolveRunForMessage`。
   */
  const runForMessage = (i: number) => resolveRunForMessage(msgs, runsState.runs, i, convId)

  const streamingAssistant = streaming && msgs.length > 0 && msgs[msgs.length - 1].role === 'assistant'
  const awaitingAssistant = streaming && !streamingAssistant
  const lastAssistantIndex = msgs.reduce((last, message, index) => message.role === 'assistant' ? index : last, -1)

  const conversationList = (
    <ConversationSidebar
              conversations={convMgr.conversations.length ? convMgr.conversations : convs}
              activeId={convId}
              query={convQuery}
              onQueryChange={(q) => {
                setConvQuery(q)
                void convMgr.list(q)
              }}
              onSelect={(id) => { void openConversation(id); setMobileConversationsOpen(false) }}
              onCreate={() => { startNewConversation(); setMobileConversationsOpen(false) }}
              onRename={(id, title) => void convMgr.rename(id, title)}
              onPin={(id, pinned) => void convMgr.pin(id, pinned)}
              onDelete={(id) => void removeConversation(id)}
            />
  )

  return (
    <div ref={viewportPanel} className={`relative flex min-h-0 gap-4 ${presentation === 'mini' ? 'h-full' : 'min-h-[240px] h-[calc(100dvh-15rem)] lg:h-[calc(100dvh-10rem)]'}`} data-testid="ai-copilot-page" data-presentation={presentation}>
      {/* 左：战术指令池（按时段分层：今日推荐 3 + 更多） */}
      {/*
        2026-10-26 二次精简（按用户指令）：预设整块去掉，左栏只保留**对话列表**，
        并给它一个可折叠开关（状态存 localStorage）。页面不再有"预设/对话"两个 tab。
      */}
      {railOpen && presentation === 'page' ? (
        <aside
          className="card hidden w-[232px] shrink-0 flex-col p-3 lg:flex"
          data-testid="copilot-conversation-rail"
        >
          <div className="flex items-center justify-between px-1 py-1">
            <span className="text-[12px] font-medium text-foreground">对话</span>
            <button
              type="button"
              onClick={() => {
                setRailOpen(false)
                writePref(window.localStorage, RAIL_KEY, '0')
              }}
              aria-label="收起对话栏"
              data-testid="copilot-rail-collapse"
              className="ui-icon-button"
            >
              <PanelLeftClose className="h-3.5 w-3.5" />
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {!mobileConversationsOpen ? conversationList : null}
          </div>
        </aside>
      ) : null}

        {/* 常驻会话列已撤除(2026-10-26): 会话列表并入左侧栏的「对话」tab, 页面不再被切成多条竖带 */}

        {/* 历史抽屉已移除(2026-10-26): 与左栏对话列表重复 */}


      {/* 右：交互工作台 */}
      <section className="card flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        {pending.length > 0 ? (
          <div className="flex flex-wrap items-center gap-2 border-b border-border bg-role-watch/5 px-3 py-2" data-testid="copilot-pending-actions">
            <span className="text-[12px] text-muted-foreground">待确认动作</span>
            {pending.slice(0, 3).map((a) => {
              const rowBusy = acting?.id === a.id
              // 任何一条动作在途都禁掉全部按钮：守卫是全局的（一次只跑一条裁决）
              const locked = acting !== null
              return (
                <span key={a.id} className="flex flex-wrap items-center gap-1 rounded-md border border-border px-2 py-0.5 text-[12px]">
                  <span className="font-mono">{a.kind === 'watchlist' ? '加自选' : a.kind === 'annotation' ? '画标注' : '盯盘提醒'}</span>
                  <span className="text-muted-foreground">{String((a.payload as Record<string, unknown>).symbol ?? '')}</span>
                  <button
                    type="button"
                    data-testid="copilot-pending-apply"
                    aria-busy={rowBusy && acting?.kind === 'apply' ? 'true' : undefined}
                    disabled={locked}
                    className="inline-flex items-center gap-1 text-role-watch hover:underline disabled:opacity-50 disabled:no-underline"
                    onClick={() => void act(a.id, 'apply')}
                  >
                    {rowBusy && acting?.kind === 'apply' ? <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" /> : null}
                    确认
                  </button>
                  <button
                    type="button"
                    data-testid="copilot-pending-reject"
                    aria-busy={rowBusy && acting?.kind === 'reject' ? 'true' : undefined}
                    disabled={locked}
                    className="inline-flex items-center gap-1 text-muted-foreground hover:underline disabled:opacity-50 disabled:no-underline"
                    onClick={() => void act(a.id, 'reject')}
                  >
                    {rowBusy && acting?.kind === 'reject' ? <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" /> : null}
                    忽略
                  </button>
                  {/* 失败就地提示：挂在这条动作旁边，原因 + 该做什么都在一句话里 */}
                  {actError?.id === a.id ? (
                    <span data-testid="copilot-pending-error" role="alert" className="basis-full text-[12px] text-role-risk">
                      {actError.message}
                    </span>
                  ) : null}
                </span>
              )
            })}
          </div>
        ) : null}
        <div className="flex flex-wrap items-center gap-1.5 border-b border-border px-3 py-1.5">
          <button type="button" className={`flex min-h-9 items-center gap-1.5 rounded-lg px-2 text-[12px] text-foreground hover:bg-s2 ${presentation === 'page' ? 'lg:hidden' : ''}`} aria-expanded={mobileConversationsOpen} aria-controls="copilot-mobile-conversations" onClick={() => setMobileConversationsOpen((value) => !value)}>
            <PanelLeftOpen className="h-4 w-4" /> 对话
          </button>
          {!railOpen && presentation === 'page' ? (
            <button
              type="button"
              onClick={() => {
                setRailOpen(true)
                writePref(window.localStorage, RAIL_KEY, '1')
              }}
              aria-label="展开对话栏"
              data-testid="copilot-rail-expand"
              className="ui-icon-button hidden lg:inline-flex"
            >
              <PanelLeftOpen className="h-3.5 w-3.5" />
            </button>
          ) : null}
          <Sparkles className="h-3.5 w-3.5 text-role-watch" />
          {/* 2026-10-26: 原来这里还有一句"筹码·共振·价量·盘口·竞价·妖股因子"，
              与输入框 placeholder 逐字重复 → 删除（页面名同样不再重复第三次） */}

          {convId !== null ? (
            <button
              type="button"
              className="rounded-md border border-border px-2 py-0.5 text-[12px] text-muted-foreground hover:text-foreground"
              onClick={startNewConversation}
            >
              新对话
            </button>
          ) : null}
          {/*
            2026-10-26 工具条收拢：原来 6 个并列小按钮 + 3 个右侧控件挤成一条，
            视觉重心全跑到顶部。现在只留高频两个（过程 / 治理），其余收进「更多」。
          */}
          {(['runs'] as const).map((tab) => (
            <button
              key={tab}
              type="button"
              data-testid={`copilot-panel-${tab}`}
              className={`h-7 rounded-md px-2 text-[12px] transition ${
                panelTab === tab
                  ? 'bg-muted text-foreground'
                  : 'text-muted-foreground hover:bg-muted hover:text-foreground'
              }`}
              onClick={() => setPanelTab((v) => (v === tab ? null : tab))}
            >
              {PANEL_TAB_LABEL[tab]}
            </button>
          ))}
          <details className="relative" data-testid="copilot-more-panels">
            <summary className="h-7 cursor-pointer list-none rounded-md px-2 text-[12px] leading-7 text-muted-foreground hover:bg-muted hover:text-foreground">
              更多
            </summary>
            <div className="absolute left-0 top-full z-30 mt-1 flex w-[120px] flex-col gap-0.5 rounded-lg border border-border bg-card p-1">
              {PANEL_TABS.filter((t) => t !== 'runs').map((tab) => (
                <button
                  key={tab}
                  type="button"
                  data-testid={`copilot-panel-${tab}`}
                  className={
                    panelTab === tab
                      ? 'rounded-md px-2 py-1 text-left text-[12px] text-role-watch'
                      : 'rounded-md px-2 py-1 text-left text-[12px] text-foreground hover:bg-muted'
                  }
                  onClick={() => setPanelTab((v) => (v === tab ? null : tab))}
                >
                  {PANEL_TAB_LABEL[tab]}
                </button>
              ))}
            </div>
          </details>
          <a
            href="/copilot-governance"
            className="h-7 rounded-md px-2 text-[12px] leading-7 text-muted-foreground hover:bg-muted hover:text-foreground"
            data-testid="copilot-governance-link"
          >
            治理
          </a>
          <div className="ml-auto flex items-center gap-2">
            {metrics ? (
              <span className="hidden text-[12px] text-muted-foreground xl:inline">
                近 {metrics.days} 天 <span className="tabular-nums text-foreground">{metrics.runs}</span> 次
              </span>
            ) : null}
            {anchorSymbol ? <PersonaBadge symbol={anchorSymbol} /> : null}
            <ModelPicker className="hidden md:block" />
          </div>
          {/* 代码输入 + 看卡已移除(2026-10-26): 用户指出与主输入框重复, 且挤占工具条 */}

        </div>

        {mobileConversationsOpen ? (
          <div id="copilot-mobile-conversations" className={`max-h-[320px] overflow-y-auto border-b border-border bg-card p-3 ${presentation === 'page' ? 'lg:hidden' : ''}`}>
            {conversationList}
          </div>
        ) : null}

        {anchorSymbol ? (
          <div
            data-testid="copilot-context-anchor"
            className="flex items-center gap-1.5 border-b border-border bg-s2 px-3 py-1.5"
          >
            <span className="text-[12px] text-muted-foreground">{CONTEXT_ANCHOR_PREFIX}</span>
            <span className="font-mono text-[12px] text-role-watch">{anchorSymbol}</span>
            <button
              type="button"
              onClick={() => {
                setBriefSymbol('')
                setAnchorSymbol('')
                /**
                 * 换标的时的图表上下文（P2 修正, 2026-10-29）：
                 * 还没送出去 → 直接丢掉（工作台那条发布也撤掉，下次进来不复活）；
                 * 已送进本会话 → 藏起来就是撒谎（后端还在每轮注入），所以保留可见条并说明，
                 * 用户要彻底不带就点条上的「清除并新开会话」。
                 */
                if (chartCommittedHere) {
                  setChartNotice('本会话创建时已带入图表快照，服务端会继续随本会话注入；要彻底不带请点上方「清除并新开会话」。')
                  return
                }
                if (chartCtx) unpublishChartContext(chartContextKey(chartCtx))
                setChartCtx(null)
              }}
              className="ml-auto text-[12px] text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
            >
              {CONTEXT_ANCHOR_CLEAR}
            </button>
          </div>
        ) : null}

        {chipCtx ? (
          <ChartContextChip
            ctx={chipCtx}
            committed={chartCommittedHere}
            /**
             * 已送入的会话不受 TTL 影响（2026-10-29 复审）：TTL 只决定"要不要再带一份新的"，
             * 那份已经落进 `initial_context` 的快照后端每轮仍在注入 —— 所以此时不能标"不会发送"。
             */
            expired={chartCtxExpired && !chartCommittedHere}
            busy={streaming || uploading || backgroundPending}
            onClear={clearChartContext}
          />
        ) : null}

        {chartNotice ? (
          <div
            data-testid="copilot-chart-context-notice"
            className="border-b border-border bg-s2 px-3 py-1 text-[12px] text-muted-foreground"
          >
            {chartNotice}
          </div>
        ) : null}

        {historyCtxUnknown ? (
          <div
            data-testid="copilot-chart-context-history-notice"
            className="border-b border-border bg-s2 px-3 py-1 text-[12px] text-muted-foreground"
          >
            {/*
              2026-10-29 三审：这句话必须写清**后果**，不只是"无法核验"。
              早先只说"可能带着快照" —— 用户看到后面照样换票追问，旧区间就被继续注入。
              现在的实际行为：只在这个会话的绑定标的上继续；换标的/判不出标的都新开会话（旧会话保留）。
            */}
            这个历史会话不是本页创建的：服务端不回传它创建时带入的页面快照/图表快照，本页无法核验它是否带着某只票的区间与价位。
            因此只在这个会话的绑定标的{convBoundSymbol ? `（${convBoundSymbol}）` : '（未绑定标的）'}上继续追问；
            一旦换到别的标的，或无法确认这次问的是不是同一只票，本轮就会新开一个不带快照的会话，当前会话保留在列表里。
          </div>
        ) : null}

        {priceAnchor ? (
          <div
            data-testid="copilot-price-anchor"
            className="flex flex-wrap items-center gap-1.5 border-b border-border px-3 py-1"
          >
            <span className="text-[12px] text-muted-foreground">K线锚点</span>
            <span className="font-mono text-[12px] tabular-nums text-role-watch">
              {safeFixed(priceAnchor.value, 2)}
            </span>
            <span className="text-[12px] text-muted-foreground">
              {anchorKindLabel(priceAnchor.kind, priceAnchor.context)}
            </span>
            {priceAnchor.symbol ? (
              <a
                href={`/stocks/${priceAnchor.symbol}`}
                className="rounded border border-role-watch/40 px-1 text-[12px] text-role-watch hover:bg-role-watch/10"
              >
                {priceAnchor.symbol} 工作台 ↗
              </a>
            ) : null}
            <button
              type="button"
              onClick={() => setPriceAnchor(null)}
              className="ml-auto text-[12px] text-muted-foreground hover:text-foreground"
            >
              清除
            </button>
          </div>
        ) : null}

        <div
          ref={scroller}
          onDragOver={(e) => {
            // 拖文件进来直接接管，避免浏览器把图片当导航打开
            if (Array.from(e.dataTransfer?.types || []).includes('Files')) e.preventDefault()
          }}
          onDrop={(e) => {
            const f = e.dataTransfer?.files?.[0]
            if (f) {
              e.preventDefault()
              void handleFile(f)
            }
          }}
          className="scrollbar min-h-0 flex-1 space-y-3 overflow-y-auto px-3 py-3"
        >
          {/* 实时运行面板(2026-10-25): 后台任务一提交就在这里显示工具流, 不再只有一行状态文案 */}
          <LiveRunPanel runId={liveRunId} />
          {/* 空态(2026-10-26): 页面本来就有一套引导(PAGE_EMPTY_HINT + PAGE_EMPTY_EXAMPLES),
              我先前又加了一套 —— 两套推荐并存是这版最明显的错。这里只把它**居中**，
              不新增第二套内容。 */}
          {briefSymbol ? (
            <CopilotBriefCard symbol={briefSymbol} market="CN" />
          ) : null}

          {!msgs.length ? (
            <div data-testid="copilot-empty-hero" className="relative flex min-h-full flex-col items-center justify-center gap-4 text-center overflow-hidden py-8">
              <AmbientGrid />
              {/* 形象只有这一个：静态柔光只做层次，不做脉冲 —— 空态没有"正在发生的事"，动效不伪造状态。
                  只用一个光晕 + 一个形象，避免与下方示例抢焦点。 */}
              <div className="relative flex items-center justify-center z-10">
                <span
                  aria-hidden="true"
                  className="pointer-events-none absolute left-1/2 top-1/2 h-[176px] w-[176px] -translate-x-1/2 -translate-y-1/2 rounded-full"
                  style={{ background: 'radial-gradient(circle, hsl(var(--role-watch) / 0.12) 0%, transparent 68%)' }}
                />
                <AssistantAvatar size={presentation === 'mini' ? 88 : 112} label="数智参谋" />
              </div>
              <p className="max-w-[440px] px-2 text-pretty text-[12px] leading-relaxed text-muted-foreground z-10">{PAGE_EMPTY_HINT}</p>
              {/* 2026-10-29 重排：原来 2x2 网格的第 4 格是"最近一次"，没记录时是一张写着
                  "还没有运行记录"的**死入口**，而且它与工具条「过程」面板是同一份数据的两个入口。
                  现在三条示例并排（窄屏竖排），最近一次降级为一行真实回链，没有记录就整块不渲染。 */}
              <ol data-testid="copilot-empty-examples" className="grid w-full max-w-[660px] list-none grid-cols-1 gap-2 sm:grid-cols-3 z-10">
                {PAGE_EMPTY_EXAMPLES.map((x, i) => (
                  <li key={x} className="flex min-w-0">
                    <button
                      type="button"
                      onClick={() => void send(x)}
                      disabled={streaming}
                      data-testid="copilot-empty-example"
                      className="card-surface group flex min-h-[84px] w-full flex-col items-start gap-1.5 rounded-xl bg-s2 p-3 text-left transition-colors hover:bg-s3 disabled:opacity-60 focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      <span className="flex w-full items-center gap-1.5 text-[12px] text-muted-foreground">
                        <span className="font-mono tabular-nums">{String(i + 1).padStart(2, '0')}</span>
                        <ChevronRight className="ml-auto h-3 w-3 shrink-0 text-muted-foreground group-hover:text-role-watch" aria-hidden="true" />
                      </span>
                      <span className="min-w-0 text-pretty text-[14px] leading-relaxed text-foreground">{x}</span>
                    </button>
                  </li>
                ))}
              </ol>
              {lastRun ? (
                <a
                  href={`/copilot/runs/${lastRun.run_id}`}
                  data-testid="copilot-empty-last-run"
                  className="flex w-full max-w-[660px] items-center gap-1.5 px-1 text-[12px] text-muted-foreground hover:text-foreground z-10"
                >
                  <span className="shrink-0">最近一次</span>
                  <span className="shrink-0" aria-hidden="true">·</span>
                  <span className="min-w-0 truncate text-left">{lastRun.question || '未命名问题'}</span>
                  <span className="shrink-0 tabular-nums">
                    · {lastRun.status} · {lastRun.tool_calls ?? '--'} 次工具
                  </span>
                  <ChevronRight className="h-3 w-3 shrink-0" aria-hidden="true" />
                </a>
              ) : null}
            </div>
          ) : null}

          {msgs.map((m, i) => {
            const isLatestAssistant = m.role === 'assistant' && i === lastAssistantIndex && !awaitingAssistant
            const liveDraft = m.role === 'assistant' && streamingAssistant && i === msgs.length - 1
            return (
            <div key={`${m.role}-${i}`} className={m.role === 'user' ? 'flex justify-end text-right' : 'flex items-start gap-3'} data-assistant-bubble={m.role === 'assistant' ? isLatestAssistant ? 'latest' : 'history' : undefined}>
              {m.role === 'assistant' && (isLatestAssistant ? <AssistantAvatar size={40} busy={streamingAssistant || backgroundPending} /> : <span className="w-10 shrink-0" aria-hidden="true" />)}
              <StreamingBeam active={liveDraft} className={m.role === 'assistant' ? 'min-w-0 flex-1' : ''}>
              <div
                className={'card-surface ' + (
 m.role === 'user'
 ? 'inline-block max-w-[86%] rounded-xl bg-s3 px-3 py-2 text-[14px]'
 : 'min-w-0 flex-1 rounded-xl bg-s2 px-3 py-3 text-[14px] leading-relaxed'
 )}
              >
                {m.role === 'user' ? (
                  m.content
                ) : (
                  (() => {
                    const displayContent = publicAssistantContent(m.content)
                    const parsed = parseAnswer(displayContent)
                    const symbolInMsg = extractSymbolChips(m.content)[0] || anchorSymbol
                    /** 最后一条助手气泡还在写（草稿）：它的身份与证据都还没定稿。 */
                    const liveDraft = streamingAssistant && i === msgs.length - 1
                    // 定稿消息带自己的痕；流式中的最后一条用实时态（定稿瞬间原地换成固化值）
                    const strip = m.readiness ?? (liveDraft ? readiness : null)
                    /**
                     * 溯源条（2026-10-29 P1 接线）：只有 structured 回答才有「依据」可言。
                     *  - 草稿不挂（`live`）：终稿没到，回答与证据都没定稿；
                     *  - readiness 只给这条消息**自己的**固化值，绝不把当前流的实时态
                     *    挂到历史消息上（那就是拿这一轮的取数去证明上一轮的结论）；
                     *  - 未拿到终稿（incomplete）也不挂缺口数字：这一轮没有「完成记录」。
                     *  - run 列表还在拉（且手上一条都没）时先不挂：此刻说「未关联」只是
                     *    「还没查到」，等结果回来再按真实情况写。
                     */
                    const runsUnknown = runsState.loading && runsState.runs.length === 0
                    const trace = parsed.structured && !runsUnknown
                      ? buildMessageAnswerTrace({
                          msgs,
                          runs: runsState.runs,
                          details: runsState.details,
                          index: i,
                          conversationId: convId,
                          live: liveDraft,
                          readiness: m.incomplete ? null : m.readiness ?? null,
                        })
                      : null
                    return (
                      <>
                        {strip && strip.items.length ? (
                          <div className="mb-1.5">
                            <EvidenceReadinessStrip
                              items={strip.items}
                              done={strip.done}
                              total={strip.total}
                              failed={strip.failed}
                            />
                          </div>
                        ) : null}
                        {parsed.structured ? (
                          <AnswerCard
                            parsed={parsed}
                            symbol={symbolInMsg}
                            trace={trace ?? undefined}
                            onFollowUp={(q) => void send(q)}
                            onAnchoredPrice={(a) =>
                              setPriceAnchor({ ...a, symbol: symbolInMsg || undefined })
                            }
                          />
                        ) : (
                          <SafeMarkdown>{displayContent}</SafeMarkdown>
                        )}
                        {m.incomplete ? (
                          <p data-testid="copilot-stream-incomplete" className="mt-1 text-[12px] text-role-risk">
                            未拿到服务端终稿，以上内容可能不完整
                          </p>
                        ) : null}
                        {(() => {
                          const r = runForMessage(i)
                          if (!r) return null
                          const det = runsState.details[r.run_id]
                          return (
                            <InlineSteps
                              run={r}
                              association={m.runId === r.run_id ? 'run-id' : 'question-text'}
                              steps={det?.steps as never}
                              loading={runsState.detailLoading}
                              failed={Boolean(runsState.error) && !det}
                              onLoad={() => void runsState.loadDetail(r.run_id)}
                            />
                          )
                        })()}
                        {m.role === 'assistant' && m.evidenceAudit ? (
                          <EvidenceRowAuditPanel audit={m.evidenceAudit} />
                        ) : null}
                      </>
                    )
                  })()
                )}
                {m.role === 'user' && m.attachment ? (
                  <span className="mt-1 block text-[12px] text-muted-foreground">
                    📎 {m.attachment.name}
                  </span>
                ) : null}
                {m.role === 'assistant'
                  ? extractSymbolChips(m.content).map((s) => (
                      <a
                        key={s}
                        href={`/stocks/${s}`}
                        className="mr-1 mt-1 inline-block rounded border border-role-watch/40 px-1.5 text-[12px] text-role-watch"
                      >
                        {s} 工作台 ↗
                      </a>
                    ))
                  : null}
                <MessageActions
                  role={m.role}
                  copyText={m.role === 'assistant' ? publicAssistantContent(m.content) : m.content}
                  canRegenerate={m.role === 'assistant' && i === msgs.length - 1 && !streaming}
                  onEdit={m.role === 'user' ? () => setInput(m.content) : undefined}
                  onCopy={() => {}}
                  onRegenerate={() => {
                    const last = [...msgs].reverse().find((x) => x.role === 'user')
                    if (last) void send(last.content)
                  }}
                />
              </div>
              </StreamingBeam>
            </div>
            )
          })}

          {/* 首字未到、但已经在取数：齐套度条先挂在流尾（气泡一出现就挪进气泡顶部）。
              能认出工具的 stage 不进这条 spinner —— 它已经被上面的 pill 取代。 */}
          {readiness.items.length && !streamingAssistant && !awaitingAssistant ? (
            <EvidenceReadinessStrip
              items={readiness.items}
              done={readiness.done}
              total={readiness.total}
              failed={readiness.failed}
            />
          ) : null}
          {awaitingAssistant ? (
            <div className="flex items-start gap-3" data-assistant-bubble="pending">
              <AssistantAvatar size={40} busy />
              <div className="min-w-0 flex-1 space-y-2 rounded-xl bg-s2 px-3 py-3 text-[14px] text-muted-foreground" role="status">
                {!!readiness.items.length && <EvidenceReadinessStrip items={readiness.items} done={readiness.done} total={readiness.total} failed={readiness.failed} />}
                <p>{parseStageLabel(stage) ? '正在整理分析证据…' : stage || '正在分析数据…'}</p>
              </div>
            </div>
          ) : stage && !parseStageLabel(stage) ? (
            <div className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" />
              {stage}
            </div>
          ) : null}
          {error ? (
            <div className="card-surface bg-card rounded px-2 py-1.5 text-[12px] text-role-risk">
              {error}
            </div>
          ) : null}
          {streamNotice ? (
            <div data-testid="copilot-stream-notice" className="card-surface bg-card rounded px-2 py-1.5 text-[12px] text-muted-foreground">
              {streamNotice}
            </div>
          ) : null}
        </div>

        <div className="shrink-0 border-t border-border bg-card p-3" data-testid="copilot-composer">
          {/* 预设入口已整体移除(2026-10-26): 用户裁定不需要, 空态示例已承担引导 */}

          {/* 待发送附件预览（图片显示缩略图，其它显示文件名） */}
          {attach ? (
            <div className="card-surface mb-2 flex items-center gap-2 rounded-lg bg-s2 px-2 py-1.5">
              {attach.imageData ? (
                <img
                  src={attach.imageData}
                  alt={attach.name}
                  className="h-9 w-9 shrink-0 rounded object-cover"
                />
              ) : (
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded bg-s3 text-[12px] text-muted-foreground">
                  {attach.name.split('.').pop()?.slice(0, 4).toUpperCase() || 'FILE'}
                </span>
              )}
              <div className="min-w-0 flex-1">
                <div className="truncate text-[12px] text-foreground">{attach.name}</div>
                <div className="text-[12px] text-muted-foreground">
                  {attach.text
                    ? `${attach.text.length} 字已解析，可一起提问`
                    : attach.imageData
                      ? ATTACH_IMAGE_READY
                      : ATTACH_EMPTY}
                </div>
              </div>
              <button
                type="button"
                onClick={() => setAttach(null)}
                aria-label={ATTACH_REMOVE}
                title={ATTACH_REMOVE}
                data-testid="copilot-attach-remove"
                className="flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-s3 hover:text-foreground"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          ) : null}

          {uploading ? (
            <div className="mb-1.5 flex items-center gap-1.5 text-[12px] text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" />
              {ATTACH_PARSING}
            </div>
          ) : null}

          <div className="flex flex-col gap-2">
            <input
              ref={fileRef}
              type="file"
              data-testid="copilot-attach-input"
              className="hidden"
              accept="image/*,.pdf,.xlsx,.xls,.csv,.txt,.md,.docx"
              onChange={(e) => {
                const f = e.target.files?.[0]
                e.target.value = ''
                void handleFile(f)
              }}
            />
            <div className="relative min-w-0">
              {mentionHit ? (
                <MentionPicker
                  tab={mentionTab}
                  onTab={(t) => {
                    setMentionTab(t)
                    setMentionIdx(0)
                  }}
                  stocks={filterMentions(mentionPool.stocks, mentionHit.query)}
                  agents={filterMentions(mentionPool.agents, mentionHit.query)}
                  active={mentionIdx}
                  onHover={setMentionIdx}
                  onPick={pickMention}
                  loading={mentionLoading}
                />
              ) : null}
              {mentions.length ? (
                <div className="mb-1 flex flex-wrap gap-1" data-testid="copilot-mention-chips">
                  {mentions.map((m) => (
                    <span
                      key={mentionKey(m)}
                      className="flex items-center gap-1 rounded-full border border-role-watch/40 bg-role-watch/10 px-2 py-0.5 text-[12px] text-role-watch"
                    >
                      {m.type === 'stock' ? `@${m.name || m.symbol}` : `@${m.label}`}
                      <button
                        type="button"
                        aria-label="移除"
                        onClick={() => setMentions((list) => list.filter((x) => mentionKey(x) !== mentionKey(m)))}
                        className="text-role-watch/70 hover:text-role-watch"
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              ) : null}
              <textarea
                ref={inputRef}
                value={input}
                onChange={(e) => {
                  const v = e.target.value
                  setInput(v)
                  if (composingRef.current) {
                    setMentionHit(null)
                    return
                  }
                  const caret = e.target.selectionStart ?? v.length
                  const hit = detectMention(v, caret)
                  setMentionHit(hit)
                  if (hit) setMentionIdx(0)
                }}
                onCompositionStart={() => {
                  composingRef.current = true
                  setMentionHit(null)
                }}
                onCompositionEnd={(e) => {
                  composingRef.current = false
                  // 组合结束必须补一次检测：中文输入法里 `@`/`@北方` 是当作组合输入提交的，
                  // 组合期我们刻意不弹面板，若这里不再检测，就只能等用户再敲一个字符才出现
                  // —— 表现就是「只有英文 @ 才会带出面板」。
                  const el = e.target as HTMLTextAreaElement
                  const hit = detectMention(el.value, el.selectionStart ?? el.value.length)
                  setMentionHit(hit)
                  if (hit) setMentionIdx(0)
                }}
                onPaste={(e) => {
                  // 直接粘贴截图（截图工具 → Ctrl+V）是最常见的用法，不能只支持点选文件
                  const item = Array.from(e.clipboardData?.items || []).find((it) =>
                    it.type.startsWith('image/'),
                  )
                  const file = item?.getAsFile()
                  if (file) {
                    e.preventDefault()
                    void handleFile(file)
                  }
                }}
                onKeyDown={(e) => {
                  // 手机的回车键用于换行，发送由明确的发送按钮完成。
                  if (window.innerWidth < 768 && e.key === 'Enter' && !e.ctrlKey && !e.metaKey) return
                  if (mentionHit && mentionRows.length) {
                    if (e.key === 'ArrowDown') {
                      e.preventDefault()
                      setMentionIdx((i) => (i + 1) % mentionRows.length)
                      return
                    }
                    if (e.key === 'ArrowUp') {
                      e.preventDefault()
                      setMentionIdx((i) => (i - 1 + mentionRows.length) % mentionRows.length)
                      return
                    }
                    if (e.key === 'Enter') {
                      e.preventDefault()
                      pickMention(mentionRows[Math.min(mentionIdx, mentionRows.length - 1)])
                      return
                    }
                    if (e.key === 'Escape') {
                      e.preventDefault()
                      setMentionHit(null)
                      return
                    }
                  }
                  if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                    e.preventDefault()
                    void send(input)
                  }
                }}
                rows={1}
                placeholder={
                  attach ? '补充说明…' : '问点什么…'
                }
                aria-label="分析问题"
                className="scrollbar block max-h-40 min-h-11 w-full resize-none rounded-lg border border-input bg-card px-3 py-2 text-[16px] leading-relaxed md:min-h-[80px] md:text-[14px]"
              />
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => fileRef.current?.click()}
                disabled={streaming || uploading || backgroundPending}
                aria-label={ATTACH_HINT}
                title={ATTACH_HINT}
                data-testid="copilot-attach"
                className="flex h-11 w-11 md:h-9 md:w-9 shrink-0 items-center justify-center rounded-lg border border-border text-muted-foreground hover:bg-s2 hover:text-foreground disabled:opacity-40"
              >
                {uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Paperclip className="h-4 w-4" />}
              </button>
              <span className="hidden text-[12px] text-muted-foreground md:inline">Enter 发送 · Shift+Enter 换行</span>
              <button
                type="button"
                onClick={() => setBgMode((v) => !v)}
                aria-pressed={bgMode}
                data-testid="copilot-bg-toggle"
                title="后台执行：提交后即可离开页面，回来在会话里看结果"
                className={`ml-auto flex h-11 shrink-0 items-center rounded-lg px-3 text-[12px] transition-colors md:h-9 ${
                  bgMode
                    ? 'bg-role-watch/20 text-role-watch'
                    : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                {bgMode ? '后台中' : '后台'}
              </button>
              <button
                type="button"
                onClick={() => void send(input)}
                /**
                 * 后台任务在飞时也禁用（2026-10-29 复审）：那一轮还没结束、`convId` 可能
                 * 已经被判作废，再发一条极易落进旧会话（带旧图快照）。等它结束再问。
                 */
                disabled={streaming || uploading || backgroundPending || convLoading || (!input.trim() && !attach)}
                title={
                  backgroundPending
                    ? '后台任务进行中：等它结束后再提问（避免落进旧会话）'
                    : convLoading
                      ? '正在打开那个会话：等它回来再问（现在发出去会落进上一个会话）'
                      : undefined
                }
                className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-role-watch/15 text-role-watch hover:bg-role-watch/25 disabled:opacity-40 md:h-9 md:w-9"
                aria-label={SEND}
              >
                {streaming || backgroundPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
              </button>
            </div>
          </div>
        </div>
      </section>

      {panelTab ? (
        <aside
          className="absolute inset-y-0 right-0 z-20 flex w-[320px] flex-col border-l border-border bg-s1 backdrop-blur"
          data-testid="copilot-side-panel"
        >
          <div className="flex items-center justify-between py-2 pl-3 pr-1">
            <span className="text-[12px] font-medium">{PANEL_TAB_TITLE[panelTab]}</span>
            <button type="button" aria-label="关闭" onClick={() => setPanelTab(null)}>
              <X className="h-3.5 w-3.5 text-muted-foreground" />
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {panelTab === 'runs' ? (
              <ToolTimeline
                runs={runsState.runs}
                details={runsState.details}
                onLoadDetail={(rid) => void runsState.loadDetail(rid)}
              />
            ) : panelTab === 'memory' ? (
              <MemoryPanel />
            ) : panelTab === 'schedule' ? (
              <SchedulePanel />
            ) : (
              <ProposalPanel />
            )}
          </div>
        </aside>
      ) : null}
    </div>
  )
}
