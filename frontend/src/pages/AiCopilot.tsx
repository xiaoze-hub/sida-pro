import { useCallback, useEffect, useRef, useState } from 'react'
import { safeFixed } from '@/lib/format'
import {
  applyPendingAction,
  getCopilotMetrics,
  getPendingActions,
  rejectPendingAction,
  type CopilotMetrics,
  type PendingAction,
} from '@/lib/copilot/api'
import { ChevronRight, Loader2, PanelLeftClose, PanelLeftOpen, Paperclip, Send, Sparkles, X } from 'lucide-react'
import { chatApi } from '@panwatch/api'
import SafeMarkdown from '@/components/SafeMarkdown'
import { AnswerCard } from '@/components/copilot/AnswerCard'
import InlineSteps from '@/components/copilot/InlineSteps'
import { matchRunForMessage } from '@/lib/copilot/match-run'
import { EvidenceReadinessStrip } from '@/components/copilot/EvidenceReadinessStrip'
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
import { extractSymbolChips, marketOf } from '@/lib/copilot/symbolChips'
import { applyMention, detectMention, filterMentions, mentionKey, type MentionHit } from '@/lib/copilot/mentions'
import { getCopilotMentions, type MentionAgent, type MentionStock } from '@/lib/copilot/api'
import { MentionPicker, type MentionTab } from '@/components/copilot/MentionPicker'
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

type Msg = {
  role: 'user' | 'assistant'
  content: string
  /** 用户消息附带的文件名（气泡里显示小 chip，正文不重复文件名太长）。 */
  attachment?: { name: string; isImage: boolean }
  /** 助手消息定稿时固化的证据齐套度（流中长在页面上，答完留在回答顶部可回看）。 */
  readiness?: ReadinessState
}

/** 6 位 A 股代码（含北交所 4/8 开头）才允许查参谋卡，避免把日期/金额当代码。 */
const SYMBOL_RE = /^[03468]\d{5}$/

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
export default function AiCopilotPage() {
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
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
  // 左栏（对话列表）可折叠：默认展开，状态持久化
  const [railOpen, setRailOpen] = useState<boolean>(() =>
    readBoolPref(typeof window === 'undefined' ? null : window.localStorage, RAIL_KEY),
  )
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
  const runsState = useCopilotRuns({ conversationId: convId, enabled: true })
  const [convs, setConvs] = useState<{ id: number; title: string; created_at: string }[]>([])
  const [error, setError] = useState<string | null>(null)
  /** 上下文锚点（?symbol= 带进来即显示），不触发参谋卡请求 */
  const [anchorSymbol, setAnchorSymbol] = useState('')
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
  const abortRef = useRef<AbortController | null>(null)
  const scroller = useRef<HTMLDivElement>(null)

  /**
   * 上下文带入（P0）：从个股工作台/外部链接进来时带 `?symbol=600111`，
   * 页面直接进入「正在看」状态，用户不必再输一次代码；带 `?q=` 时预填问题。
   * 用 URLSearchParams 而非 router hook：本页单测裸渲染不带 Router。
   */
  useEffect(() => {
    if (typeof window === 'undefined') return
    const sp = new URLSearchParams(window.location.search)
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
  }, [])

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight })
  }, [msgs, stage])

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
      setConvs((rows || []).map((c) => ({ id: c.id, title: c.title || '未命名对话', created_at: c.created_at })))
    } catch {
      setConvs([])
    }
  }, [])

  useEffect(() => {
    void loadHistory()
  }, [loadHistory])

  const openConversation = useCallback(async (id: number) => {
    try {
      const detail = await chatApi.getConversation(id)
      setConvId(id)
      setMsgs(
        (detail.messages || [])
          .filter((m) => m.role === 'user' || m.role === 'assistant')
          .map((m) => ({ role: m.role as 'user' | 'assistant', content: m.content })),
      )
      const sym = detail.conversation?.stock_symbol || ''
      if (sym) setAnchorSymbol(sym)
      setError(null)
    } catch (e) {
      setError(`打开历史对话失败：${e instanceof Error ? e.message : '未知错误'}`)
    }
  }, [])

  const startNewConversation = useCallback(() => {
    setConvId(null)
    setMsgs([])
    setError(null)
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
    async (question: string, attachment?: { name: string; isImage: boolean }) => {
      setMsgs((m) => [...m, { role: 'user', content: question, attachment }])
      setInput('')
      setAttach(null)
      setMentions([])
      setMentionHit(null)
      setError(null)
      const placeholder = `⏳ ${progressText(null)}`
      setMsgs((m) => [...m, { role: 'assistant', content: placeholder }])
      const patch = (content: string) =>
        setMsgs((m) => {
          const last = m[m.length - 1]
          if (!last || last.role !== 'assistant') return m
          return [...m.slice(0, -1), { ...last, content }]
        })

      try {
        const submitted = await submitCopilotRun(question, convId)
        setLiveRunId(submitted.run_id)
        const deadline = Date.now() + 10 * 60 * 1000
        let last = null as Awaited<ReturnType<typeof getCopilotRunStatus>> | null
        while (Date.now() < deadline) {
          await new Promise((r) => setTimeout(r, 2000))
          last = await getCopilotRunStatus(submitted.run_id)
          patch(`⏳ ${progressText(last)}`)
          if (isTerminalStatus(last.status)) {
            // 竞态（2026-10-25 生产实测）：轨迹先写终态，执行体随后才补 conversation_id，
            // 前端一看到终态就断开会读到 null。补拉几次，拿到会话 id 为止（最多 ~2s）。
            for (let k = 0; k < 4 && !last.conversation_id; k += 1) {
              await new Promise((r) => setTimeout(r, 500))
              last = await getCopilotRunStatus(submitted.run_id)
            }
            break
          }
        }
        if (!last || !isTerminalStatus(last.status)) {
          patch('⚠️ 后台任务超过 10 分钟仍未结束，已停止跟踪（可在「参谋任务」页查看最终状态）')
          return
        }
        if (last.status === 'ok' || last.status === 'degraded' || last.status === 'succeeded') {
          const cid = last.conversation_id ?? convId
          if (cid) {
            // 正文已由服务端落库，重新拉一次会话即可（不在前端拼答案）
            if (cid !== convId) setConvId(cid)
            await openConversation(cid)
            if (last.status === 'degraded') {
              setError('本轮部分工具失败，结论可能不完整（展开「过程」可看是哪一步）')
            }
          } else {
            patch('✅ 已完成，但没拿到会话 id（可在「参谋任务」页查看）')
          }
        } else {
          patch(`❌ ${progressText(last)}`)
        }
      } catch (e) {
        patch(`❌ 提交失败：${e instanceof Error ? e.message : String(e)}`)
      }
    },
    [convId, openConversation],
  )

  const send = useCallback(
    async (text: string) => {
      const words = text.trim()
      // 只有附件没打字也允许发（截图直接问"这是什么"），所以附件在场时不要求文字
      if ((!words && !attach) || streaming || uploading) return
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
      // 后台模式：附件暂不支持（附件要先过视觉转写，走原流式路径）
      if (bgMode && !attach) {
        await runInBackground(content, sentAttachment)
        return
      }
      setError(null)
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
      try {
        // 标的解析优先级：@ 指定 > 本条文字里的代码 > 页面锚点（正在看 XXX）
        const mentionedStock = mentions.find((m) => m.type === 'stock') as MentionStock | undefined
        const symInText = extractSymbolChips(words)[0]
        const sym = mentionedStock?.symbol || symInText || anchorSymbol
        if (sym && sym !== anchorSymbol) setAnchorSymbol(sym)

        let id = convId
        if (!id) {
          const conv = await chatApi.createConversation(
            sym
              ? {
                  stock_symbol: sym,
                  stock_market: marketOf(sym),
                  initial_context: `用户从【数智参谋】一级页进入，当前标的 ${sym}。`,
                }
              : { initial_context: '用户从【数智参谋】一级页进入，尚未指定标的。' },
          )
          id = conv.id
          setConvId(id)
        }
        const ac = new AbortController()
        abortRef.current = ac
        await chatApi.sendMessageStream(
          id,
          content,
          {
            onStage: (s) => {
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
              assistant += d
              setStage('')
              setMsgs((m) => {
                const last = m[m.length - 1]
                if (last?.role === 'assistant') {
                  return [...m.slice(0, -1), { role: 'assistant', content: assistant }]
                }
                return [...m, { role: 'assistant', content: assistant }]
              })
            },
            onError: (e) => setError(e),
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
      } catch (e) {
        setError(e instanceof Error ? e.message : '对话失败')
      } finally {
        setStreaming(false)
        // 2026-10-25: 答完刷新 run 列表 —— 否则内联步骤条摘要用的是打开会话那一刻的快照
        // （实测新回答显示 "0 步 · -- · running"，因为 run 行在发问前就抓好了）
        runsState.reload()
        // 后端 run 行的最终状态可能比 SSE 收尾晚一拍，再补一次
        window.setTimeout(() => runsState.reload(), 1600)
        setStage('')
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
    },
    [attach, convId, anchorSymbol, mentions, streaming, uploading, runsState, bgMode, runInBackground],
  )

  // 动作回写(P1): AI 只能提议, 用户点确认才落库 —— 融合的硬指标是"能改页面"。
  const [metrics, setMetrics] = useState<CopilotMetrics | null>(null)
  const [pending, setPending] = useState<PendingAction[]>([])
  const loadPending = useCallback(() => {
    void getPendingActions()
      .then((rows) => setPending(Array.isArray(rows) ? rows : []))
      .catch(() => setPending([]))
  }, [])
  useEffect(() => {
    loadPending()
    const timer = window.setInterval(loadPending, 20_000)
    return () => window.clearInterval(timer)
  }, [loadPending])

  // 治理块（P3, 2026-10-20）：近 7 天运行数 / 成功率 / 平均工具数 / P95 耗时。
  useEffect(() => {
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
  }, [])

  const act = useCallback(
    async (id: number, kind: 'apply' | 'reject') => {
      try {
        if (kind === 'apply') await applyPendingAction(id)
        else await rejectPendingAction(id)
      } finally {
        loadPending()
      }
    },
    [loadPending],
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
    const onKey = (e: KeyboardEvent) => {
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
  }, [input, send, streaming, startNewConversation, setPanelTab])

  const runForMessage = (i: number) => matchRunForMessage(msgs, runsState.runs, i)

  const streamingAssistant = streaming && msgs.length > 0 && msgs[msgs.length - 1].role === 'assistant'

  return (
    <div className="relative flex h-[calc(100vh-9rem)] gap-3" data-testid="ai-copilot-page">
      {/* 左：战术指令池（按时段分层：今日推荐 3 + 更多） */}
      {/*
        2026-10-26 二次精简（按用户指令）：预设整块去掉，左栏只保留**对话列表**，
        并给它一个可折叠开关（状态存 localStorage）。页面不再有"预设/对话"两个 tab。
      */}
      {railOpen ? (
        <aside
          className="hidden w-[248px] shrink-0 flex-col lg:flex"
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
              className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
            >
              <PanelLeftClose className="h-3.5 w-3.5" />
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            <ConversationSidebar
              conversations={convMgr.conversations.length ? convMgr.conversations : convs}
              activeId={convId}
              query={convQuery}
              onQueryChange={(q) => {
                setConvQuery(q)
                void convMgr.list(q)
              }}
              onSelect={(id) => void openConversation(id)}
              onCreate={startNewConversation}
              onRename={(id, title) => void convMgr.rename(id, title)}
              onPin={(id, pinned) => void convMgr.pin(id, pinned)}
              onDelete={(id) => void convMgr.remove(id)}
            />
          </div>
        </aside>
      ) : null}

        {/* 常驻会话列已撤除(2026-10-26): 会话列表并入左侧栏的「对话」tab, 页面不再被切成多条竖带 */}

        {/* 历史抽屉已移除(2026-10-26): 与左栏对话列表重复 */}


      {/* 右：交互工作台 */}
      <section className="flex min-w-0 flex-1 flex-col rounded-xl border border-border/60 bg-s1">
        {pending.length > 0 ? (
          <div className="flex flex-wrap items-center gap-2 border-b border-border/40 bg-role-watch/5 px-3 py-2" data-testid="copilot-pending-actions">
            <span className="text-[11px] text-muted-foreground">待确认动作</span>
            {pending.slice(0, 3).map((a) => (
              <span key={a.id} className="flex items-center gap-1 rounded-md border border-border/60 px-2 py-0.5 text-[11px]">
                <span className="font-mono">{a.kind === 'watchlist' ? '加自选' : a.kind === 'annotation' ? '画标注' : '盯盘提醒'}</span>
                <span className="text-muted-foreground">{String((a.payload as Record<string, unknown>).symbol ?? '')}</span>
                <button type="button" className="text-role-watch hover:underline" onClick={() => void act(a.id, 'apply')}>确认</button>
                <button type="button" className="text-muted-foreground hover:underline" onClick={() => void act(a.id, 'reject')}>忽略</button>
              </span>
            ))}
          </div>
        ) : null}
        <div className="flex flex-wrap items-center gap-1.5 border-b border-border/40 px-3 py-1.5">
          {!railOpen ? (
            <button
              type="button"
              onClick={() => {
                setRailOpen(true)
                writePref(window.localStorage, RAIL_KEY, '1')
              }}
              aria-label="展开对话栏"
              data-testid="copilot-rail-expand"
              className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
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
              className="rounded-md border border-border/60 px-2 py-0.5 text-[11px] text-muted-foreground hover:text-foreground"
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
                      ? 'rounded-md px-2 py-1 text-left text-[11px] text-role-watch'
                      : 'rounded-md px-2 py-1 text-left text-[11px] text-foreground hover:bg-muted'
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
              <span className="hidden text-[11px] text-muted-foreground xl:inline">
                近 {metrics.days} 天 <span className="tabular-nums text-foreground">{metrics.runs}</span> 次
              </span>
            ) : null}
            {anchorSymbol ? <PersonaBadge symbol={anchorSymbol} /> : null}
            <ModelPicker className="hidden md:block" />
          </div>
          {/* 代码输入 + 看卡已移除(2026-10-26): 用户指出与主输入框重复, 且挤占工具条 */}

        </div>

        {anchorSymbol ? (
          <div
            data-testid="copilot-context-anchor"
            className="flex items-center gap-1.5 border-b border-border/40 bg-s2 px-3 py-1.5"
          >
            <span className="text-[10px] text-muted-foreground">{CONTEXT_ANCHOR_PREFIX}</span>
            <span className="font-mono text-[11px] text-role-watch">{anchorSymbol}</span>
            <button
              type="button"
              onClick={() => {
                setBriefSymbol('')
                setAnchorSymbol('')
              }}
              className="ml-auto text-[10px] text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
            >
              {CONTEXT_ANCHOR_CLEAR}
            </button>
          </div>
        ) : null}

        {priceAnchor ? (
          <div
            data-testid="copilot-price-anchor"
            className="flex flex-wrap items-center gap-1.5 border-b border-border/40 px-3 py-1"
          >
            <span className="text-[10px] text-muted-foreground">K线锚点</span>
            <span className="font-mono text-[11px] tabular-nums text-role-watch">
              {safeFixed(priceAnchor.value, 2)}
            </span>
            <span className="text-[10px] text-muted-foreground">
              {anchorKindLabel(priceAnchor.kind, priceAnchor.context)}
            </span>
            {priceAnchor.symbol ? (
              <a
                href={`/stocks/${priceAnchor.symbol}`}
                className="rounded border border-role-watch/40 px-1 text-[10px] text-role-watch hover:bg-role-watch/10"
              >
                {priceAnchor.symbol} 工作台 ↗
              </a>
            ) : null}
            <button
              type="button"
              onClick={() => setPriceAnchor(null)}
              className="ml-auto text-[10px] text-muted-foreground hover:text-foreground"
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
          className="scrollbar flex-1 space-y-3 overflow-y-auto px-3 py-3"
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
            <div className="flex min-h-[300px] flex-1 flex-col items-center justify-center gap-3 text-center">
              <div className="text-[12px] text-muted-foreground">{PAGE_EMPTY_HINT}</div>
              {/* w-full + items-stretch: 之前是 items-start, 按钮 w-full 在居中容器里宽度塌成 0,
                  结果只剩右侧 › 可见(视觉复核发现的"空描边横条") */}
              <div className="flex w-full max-w-[420px] flex-col items-stretch gap-1.5">
                {PAGE_EMPTY_EXAMPLES.map((x) => (
                  <button
                    key={x}
                    type="button"
                    onClick={() => void send(x)}
                    disabled={streaming}
                    data-testid="copilot-empty-example"
                    className="group flex w-full items-center gap-2 rounded-lg border border-role-watch/30 bg-s2 px-2.5 py-1.5 text-left text-[12px] text-foreground transition-colors hover:border-role-watch/60 hover:bg-s3 disabled:opacity-60"
                  >
                    <span className="min-w-0 flex-1">{x}</span>
                    <ChevronRight className="h-3 w-3 shrink-0 text-role-watch/70" />
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          {msgs.map((m, i) => (
            <div key={`${m.role}-${i}`} className={m.role === 'user' ? 'text-right' : ''}>
              <div
                className={
                  m.role === 'user'
                    ? 'inline-block max-w-[86%] rounded-lg bg-s3 px-2.5 py-1.5 text-[12px]'
                    : 'rounded-lg border border-border/50 bg-s2 px-2.5 py-2 text-[12px] leading-relaxed'
                }
              >
                {m.role === 'user' ? (
                  m.content
                ) : (
                  (() => {
                    const parsed = parseAnswer(m.content)
                    const symbolInMsg = extractSymbolChips(m.content)[0] || anchorSymbol
                    // 定稿消息带自己的痕；流式中的最后一条用实时态（定稿瞬间原地换成固化值）
                    const strip =
                      m.readiness ?? (streamingAssistant && i === msgs.length - 1 ? readiness : null)
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
                            onFollowUp={(q) => void send(q)}
                            onAnchoredPrice={(a) =>
                              setPriceAnchor({ ...a, symbol: symbolInMsg || undefined })
                            }
                          />
                        ) : (
                          <SafeMarkdown>{m.content}</SafeMarkdown>
                        )}
                        {(() => {
                          const r = runForMessage(i)
                          if (!r) return null
                          const det = runsState.details[r.run_id]
                          return (
                            <InlineSteps
                              run={r}
                              steps={det?.steps as never}
                              loading={runsState.detailLoading}
                              failed={Boolean(runsState.error) && !det}
                              onLoad={() => void runsState.loadDetail(r.run_id)}
                            />
                          )
                        })()}
                      </>
                    )
                  })()
                )}
                {m.role === 'user' && m.attachment ? (
                  <span className="mt-1 block text-[10px] text-muted-foreground">
                    📎 {m.attachment.name}
                  </span>
                ) : null}
                {m.role === 'assistant'
                  ? extractSymbolChips(m.content).map((s) => (
                      <a
                        key={s}
                        href={`/stocks/${s}`}
                        className="mr-1 mt-1 inline-block rounded border border-role-watch/40 px-1.5 text-[11px] text-role-watch"
                      >
                        {s} 工作台 ↗
                      </a>
                    ))
                  : null}
                <MessageActions
                  role={m.role}
                  copyText={m.content}
                  canRegenerate={m.role === 'assistant' && i === msgs.length - 1 && !streaming}
                  onEdit={m.role === 'user' ? () => setInput(m.content) : undefined}
                  onCopy={() => {}}
                  onRegenerate={() => {
                    const last = [...msgs].reverse().find((x) => x.role === 'user')
                    if (last) void send(last.content)
                  }}
                />
              </div>
            </div>
          ))}

          {/* 首字未到、但已经在取数：齐套度条先挂在流尾（气泡一出现就挪进气泡顶部）。
              能认出工具的 stage 不进这条 spinner —— 它已经被上面的 pill 取代。 */}
          {readiness.items.length && !streamingAssistant ? (
            <EvidenceReadinessStrip
              items={readiness.items}
              done={readiness.done}
              total={readiness.total}
              failed={readiness.failed}
            />
          ) : null}
          {stage && !parseStageLabel(stage) ? (
            <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" />
              {stage}
            </div>
          ) : null}
          {error ? (
            <div className="rounded border border-role-risk/40 px-2 py-1.5 text-[11px] text-role-risk">
              {error}
            </div>
          ) : null}
        </div>

        <div className="border-t border-border/40 p-2">
          {/* 预设入口已整体移除(2026-10-26): 用户裁定不需要, 空态示例已承担引导 */}

          {/* 待发送附件预览（图片显示缩略图，其它显示文件名） */}
          {attach ? (
            <div className="mb-2 flex items-center gap-2 rounded-lg border border-role-watch/35 bg-s2 px-2 py-1.5">
              {attach.imageData ? (
                <img
                  src={attach.imageData}
                  alt={attach.name}
                  className="h-9 w-9 shrink-0 rounded object-cover"
                />
              ) : (
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded bg-s3 text-[10px] text-muted-foreground">
                  {attach.name.split('.').pop()?.slice(0, 4).toUpperCase() || 'FILE'}
                </span>
              )}
              <div className="min-w-0 flex-1">
                <div className="truncate text-[11px] text-foreground">{attach.name}</div>
                <div className="text-[10px] text-muted-foreground">
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
            <div className="mb-1.5 flex items-center gap-1.5 text-[11px] text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" />
              {ATTACH_PARSING}
            </div>
          ) : null}

          <div className="flex items-end gap-2">
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              disabled={streaming || uploading}
              aria-label={ATTACH_HINT}
              title={ATTACH_HINT}
              data-testid="copilot-attach"
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border/60 text-muted-foreground hover:bg-s2 hover:text-foreground disabled:opacity-40"
            >
              {uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Paperclip className="h-4 w-4" />}
            </button>
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
            <div className="relative flex-1">
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
                      className="flex items-center gap-1 rounded-full border border-role-watch/40 bg-role-watch/10 px-2 py-0.5 text-[10px] text-role-watch"
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
              rows={2}
              placeholder={
                attach ? '补充说明（可直接发送，或继续写你的问题）' : '问筹码、共振、量价、撤单、竞价、妖股因子…（Enter 发送 / Shift+Enter 换行）'
              }
              className="scrollbar min-h-[44px] w-full resize-none rounded-lg border border-border/60 bg-s0 px-2 py-1.5 text-[12px] outline-none focus:border-role-watch/60"
            />
            </div>
            <button
              type="button"
              onClick={() => setBgMode((v) => !v)}
              aria-pressed={bgMode}
              data-testid="copilot-bg-toggle"
              title="后台执行：提交后即可离开页面，回来在会话里看结果"
              className={`flex h-9 shrink-0 items-center rounded-lg px-2 text-[11px] transition-colors ${
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
              disabled={streaming || uploading || (!input.trim() && !attach)}
              className="flex h-9 w-9 items-center justify-center rounded-lg bg-role-watch/15 text-role-watch hover:bg-role-watch/25 disabled:opacity-40"
              aria-label={SEND}
            >
              {streaming ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
            </button>
          </div>
        </div>
      </section>

      {panelTab ? (
        <aside
          className="absolute inset-y-0 right-0 z-20 flex w-[320px] flex-col border-l border-border/40 bg-s1/95 backdrop-blur"
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
