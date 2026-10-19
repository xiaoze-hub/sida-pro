import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronDown, Loader2, Paperclip, Send, Sparkles, X } from 'lucide-react'
import { chatApi } from '@panwatch/api'
import SafeMarkdown from '@/components/SafeMarkdown'
import { AnswerCard } from '@/components/copilot/AnswerCard'
import { CopilotBriefCard } from '@/components/copilot/CopilotBriefCard'
import { parseAnswer } from '@/lib/copilot/answer-blocks'
import { SESSION_LABEL, orderPresetsForSession, sessionOf } from '@/lib/copilot/presets-order'
import { getCopilotPresets } from '@/lib/copilot/api'
import { extractSymbolChips } from '@/lib/copilot/symbolChips'
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
  COPILOT_PAGE_SUBTITLE,
  COPILOT_PAGE_TITLE,
  DISCLAIMER,
  PAGE_EMPTY_EXAMPLES,
  PAGE_EMPTY_HINT,
  PRESET_FALLBACK_HINT,
  PRESET_GROUP_LABEL,
  PRESET_MORE_COLLAPSE,
  PRESET_MORE_LABEL,
  PRESET_PRIMARY_LABEL,
  SEND,
} from '@/lib/copilot/copy'
import type { CopilotPreset } from '@/lib/copilot/types'

/** 与服务端 /api/chat/upload 上限保持一致（20MB）。 */
const MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024

/** 待发送附件：解析文本 + 可选图片 data URL（图片走多模态，文本模型走解析结果）。 */
interface PendingAttachment {
  name: string
  text: string
  imageData?: string
  isImage: boolean
}

/** 指令池接口失败时的本地兜底（只影响"有没有快捷按钮"，不影响能不能提问）。 */
const LOCAL_PRESETS: CopilotPreset[] = [
  {
    id: 'chip_cost',
    icon: '🎯',
    label: '诊断底仓成本与获利盘',
    prompt:
      '用我的持仓列表，逐只给出底仓成本(COST50)、70% 筹码区间与当前获利盘比例，标出已跌破 70% 筹码下沿的票。',
    group: 'chip',
    requires_perm: null,
  },
  {
    id: 'cancel_intent',
    icon: '🛡️',
    label: '盘口撤单有没有假托单',
    prompt: '帮我看看持仓里哪几只盘口买盘撤单率异常偏高，有没有假托单的迹象，分别说明依据。',
    group: 'defense',
    requires_perm: null,
  },
  {
    id: 'wyckoff_stage',
    icon: '〰️',
    label: '现在是洗盘还是派发',
    prompt: '挑一只我持仓里的票，用日K量价结构判断现在处于哪个阶段，指出最近的弹簧试探或放量滞涨标记。',
    group: 'chip',
    requires_perm: null,
  },
  {
    id: 'auction_strong',
    icon: '🔥',
    label: '扫描今日 9:25 爆量抢筹',
    prompt:
      '扫描今天集合竞价里爆量抢筹(竞价金额>2000万且量比>2.5)的股票，按竞价金额降序给出前 10 只，并标注每只的五态判定与竞价撤单率。',
    group: 'auction',
    requires_perm: 'view_auction',
  },
]

type Msg = {
  role: 'user' | 'assistant'
  content: string
  /** 用户消息附带的文件名（气泡里显示小 chip，正文不重复文件名太长）。 */
  attachment?: { name: string; isImage: boolean }
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
  const [presets, setPresets] = useState<CopilotPreset[]>(LOCAL_PRESETS)
  const [presetFailed, setPresetFailed] = useState(false)
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [stage, setStage] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [convId, setConvId] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [symbol, setSymbol] = useState('')
  const [briefSymbol, setBriefSymbol] = useState('')
  const [showMorePresets, setShowMorePresets] = useState(false)
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
    if (SYMBOL_RE.test(sym)) {
      setSymbol(sym)
      setBriefSymbol(sym)
    }
    const q = (sp.get('q') || '').trim()
    if (q) setInput(q)
  }, [])

  useEffect(() => {
    let alive = true
    getCopilotPresets()
      .then((rows) => {
        if (!alive) return
        if (rows?.length) setPresets(rows)
      })
      .catch(() => {
        if (alive) setPresetFailed(true)
      })
    return () => {
      alive = false
    }
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

  const session = sessionOf()
  const { primary: primaryPresets, more: morePresets } = orderPresetsForSession(presets, session)

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
      setError(null)
      setMsgs((m) => [...m, { role: 'user', content, attachment: sentAttachment }])
      setInput('')
      setAttach(null)
      setStreaming(true)
      setStage('正在准备上下文…')
      let assistant = ''
      try {
        let id = convId
        if (!id) {
          const conv = await chatApi.createConversation({
            initial_context: '用户从【数智参谋】一级页进入，尚未指定标的。',
          })
          id = conv.id
          setConvId(id)
        }
        const ac = new AbortController()
        abortRef.current = ac
        await chatApi.sendMessageStream(
          id,
          content,
          {
            onStage: (s) => setStage(s),
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
        )
      } catch (e) {
        setError(e instanceof Error ? e.message : '对话失败')
      } finally {
        setStreaming(false)
        setStage('')
      }
    },
    [attach, convId, streaming, uploading],
  )

  return (
    <div className="flex h-[calc(100vh-9rem)] gap-3" data-testid="ai-copilot-page">
      {/* 左：战术指令池（按时段分层：今日推荐 3 + 更多） */}
      <aside className="hidden w-[248px] shrink-0 flex-col gap-2 lg:flex">
        <div className="flex items-center justify-between">
          <span className="text-[12px] font-medium text-muted-foreground">{PRESET_PRIMARY_LABEL}</span>
          <span className="text-[10px] text-muted-foreground/70">{SESSION_LABEL[session]}</span>
        </div>
        <div className="space-y-1.5">
          {primaryPresets.map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => void send(p.prompt)}
              disabled={streaming}
              className="w-full rounded-lg border border-role-watch/35 bg-s1 px-2.5 py-2 text-left text-[12px] hover:bg-s2 disabled:opacity-60"
            >
              <span className="mr-1">{p.icon}</span>
              {p.label}
            </button>
          ))}
          {presetFailed ? (
            <div className="rounded-lg border border-border/50 px-2.5 py-2 text-[11px] text-muted-foreground">
              {PRESET_FALLBACK_HINT}
            </div>
          ) : null}
        </div>

        {morePresets.length ? (
          <div className="space-y-1.5">
            <button
              type="button"
              onClick={() => setShowMorePresets((v) => !v)}
              data-testid="copilot-preset-more"
              className="flex w-full items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground"
            >
              <ChevronDown className={`h-3 w-3 transition-transform ${showMorePresets ? 'rotate-180' : ''}`} />
              {showMorePresets ? PRESET_MORE_COLLAPSE : `${PRESET_MORE_LABEL} ${morePresets.length}`}
            </button>
            {showMorePresets
              ? morePresets.map((p) => (
                  <button
                    key={p.id}
                    type="button"
                    onClick={() => void send(p.prompt)}
                    disabled={streaming}
                    className="w-full rounded-lg border border-border/60 bg-s1 px-2.5 py-1.5 text-left text-[11px] text-muted-foreground hover:bg-s2 hover:text-foreground disabled:opacity-60"
                  >
                    <span className="mr-1">{p.icon}</span>
                    {p.label}
                    {p.group ? (
                      <span className="ml-1 text-[10px] text-muted-foreground/70">
                        {PRESET_GROUP_LABEL[p.group] || ''}
                      </span>
                    ) : null}
                  </button>
                ))
              : null}
          </div>
        ) : null}

        <div className="mt-auto text-[10px] leading-relaxed text-muted-foreground/80">
          {DISCLAIMER}
        </div>
      </aside>

      {/* 右：交互工作台 */}
      <section className="flex min-w-0 flex-1 flex-col rounded-xl border border-border/60 bg-s1">
        <div className="flex flex-wrap items-center gap-2 border-b border-border/40 px-3 py-2">
          <Sparkles className="h-3.5 w-3.5 text-role-watch" />
          <span className="text-[12px] font-medium">{COPILOT_PAGE_TITLE}</span>
          <span className="text-[10px] text-muted-foreground">{COPILOT_PAGE_SUBTITLE}</span>
          <form
            className="ml-auto flex items-center gap-1"
            onSubmit={(e) => {
              e.preventDefault()
              const code = symbol.trim()
              setBriefSymbol(SYMBOL_RE.test(code) ? code : '')
            }}
          >
            <input
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              placeholder="输入代码看参谋卡"
              aria-label="输入股票代码查看参谋卡"
              className="h-6 w-[132px] rounded border border-border/60 bg-s0 px-1.5 font-mono text-[11px] outline-none focus:border-role-watch/60"
            />
            <button
              type="submit"
              className="h-6 rounded border border-border/50 px-1.5 text-[10px] text-muted-foreground hover:text-foreground"
            >
              看卡
            </button>
          </form>
        </div>

        {briefSymbol ? (
          <div
            data-testid="copilot-context-anchor"
            className="flex items-center gap-1.5 border-b border-border/40 bg-s2 px-3 py-1.5"
          >
            <span className="text-[10px] text-muted-foreground">{CONTEXT_ANCHOR_PREFIX}</span>
            <span className="font-mono text-[11px] text-role-watch">{briefSymbol}</span>
            <button
              type="button"
              onClick={() => {
                setBriefSymbol('')
                setSymbol('')
              }}
              className="ml-auto text-[10px] text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
            >
              {CONTEXT_ANCHOR_CLEAR}
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
          {briefSymbol ? (
            <CopilotBriefCard symbol={briefSymbol} market="CN" />
          ) : null}

          {!msgs.length ? (
            <div className="mt-4 space-y-2">
              <div className="text-[12px] text-muted-foreground">{PAGE_EMPTY_HINT}</div>
              <div className="flex flex-col items-start gap-1.5">
                {PAGE_EMPTY_EXAMPLES.map((x) => (
                  <button
                    key={x}
                    type="button"
                    onClick={() => void send(x)}
                    disabled={streaming}
                    className="rounded-full border border-border/60 px-2.5 py-1 text-left text-[12px] text-muted-foreground hover:border-role-watch/50 hover:bg-s2 hover:text-foreground disabled:opacity-60"
                  >
                    {x}
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
                    const symbolInMsg = extractSymbolChips(m.content)[0] || briefSymbol
                    return parsed.structured ? (
                      <AnswerCard parsed={parsed} symbol={symbolInMsg} onFollowUp={(q) => void send(q)} />
                    ) : (
                      <SafeMarkdown>{m.content}</SafeMarkdown>
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
              </div>
            </div>
          ))}

          {stage ? (
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
          {/* 移动端入口（<lg 左栏整块隐藏，这里补一条横向指令条） */}
          <div className="scrollbar mb-1.5 flex gap-1.5 overflow-x-auto lg:hidden">
            {primaryPresets.map((p) => (
              <button
                key={p.id}
                type="button"
                onClick={() => void send(p.prompt)}
                disabled={streaming}
                data-testid="copilot-mobile-chip"
                className="shrink-0 rounded-full border border-role-watch/35 px-2 py-0.5 text-[11px] text-role-watch disabled:opacity-50"
              >
                {p.icon} {p.label}
              </button>
            ))}
          </div>

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
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
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
                if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault()
                  void send(input)
                }
              }}
              rows={2}
              placeholder={
                attach ? '补充说明（可直接发送，或继续写你的问题）' : '问筹码、共振、量价、撤单、竞价、妖股因子…（Enter 发送 / Shift+Enter 换行）'
              }
              className="scrollbar min-h-[44px] flex-1 resize-none rounded-lg border border-border/60 bg-s0 px-2 py-1.5 text-[12px] outline-none focus:border-role-watch/60"
            />
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
    </div>
  )
}
