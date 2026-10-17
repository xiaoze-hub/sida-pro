import { useCallback, useEffect, useRef, useState } from 'react'
import { Loader2, Send, Sparkles } from 'lucide-react'
import { chatApi } from '@panwatch/api'
import SafeMarkdown from '@/components/SafeMarkdown'
import { CopilotBriefCard } from '@/components/copilot/CopilotBriefCard'
import { getCopilotPresets } from '@/lib/copilot/api'
import { extractSymbolChips } from '@/lib/copilot/symbolChips'
import {
  COPILOT_PAGE_SUBTITLE,
  COPILOT_PAGE_TITLE,
  DISCLAIMER,
  PAGE_EMPTY_EXAMPLES,
  PRESET_FALLBACK_HINT,
  PRESET_GROUP_LABEL,
  SEND,
} from '@/lib/copilot/copy'
import type { CopilotPreset } from '@/lib/copilot/types'

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

type Msg = { role: 'user' | 'assistant'; content: string }

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
  const abortRef = useRef<AbortController | null>(null)
  const scroller = useRef<HTMLDivElement>(null)

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

  const send = useCallback(
    async (text: string) => {
      const content = text.trim()
      if (!content || streaming) return
      setError(null)
      setMsgs((m) => [...m, { role: 'user', content }])
      setInput('')
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
        )
      } catch (e) {
        setError(e instanceof Error ? e.message : '对话失败')
      } finally {
        setStreaming(false)
        setStage('')
      }
    },
    [convId, streaming],
  )

  return (
    <div className="flex h-[calc(100vh-9rem)] gap-3" data-testid="ai-copilot-page">
      {/* 左：战术指令池 */}
      <aside className="hidden w-[248px] shrink-0 flex-col gap-2 lg:flex">
        <div className="text-[12px] font-medium text-muted-foreground">战术指令</div>
        <div className="space-y-1.5">
          {presets.map((p) => (
            <button
              key={p.id}
              type="button"
              onClick={() => void send(p.prompt)}
              disabled={streaming}
              className="w-full rounded-lg border border-border/60 bg-s1 px-2.5 py-2 text-left text-[12px] hover:bg-s2 disabled:opacity-60"
            >
              <span className="mr-1">{p.icon}</span>
              {p.label}
              {p.group ? (
                <span className="ml-1 text-[10px] text-muted-foreground">
                  {PRESET_GROUP_LABEL[p.group] || ''}
                </span>
              ) : null}
            </button>
          ))}
          {presetFailed ? (
            <div className="rounded-lg border border-border/50 px-2.5 py-2 text-[11px] text-muted-foreground">
              {PRESET_FALLBACK_HINT}
            </div>
          ) : null}
        </div>
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

        <div ref={scroller} className="scrollbar flex-1 space-y-3 overflow-y-auto px-3 py-3">
          {briefSymbol ? (
            <CopilotBriefCard symbol={briefSymbol} market="CN" />
          ) : null}

          {!msgs.length ? (
            <div className="mt-4 space-y-2 text-[12px] text-muted-foreground">
              <div>直接问，或点左侧战术指令。例：</div>
              <ul className="space-y-1 pl-4">
                {PAGE_EMPTY_EXAMPLES.map((x) => (
                  <li key={x}>· {x}</li>
                ))}
              </ul>
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
                {m.role === 'user' ? m.content : <SafeMarkdown>{m.content}</SafeMarkdown>}
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
          <div className="flex items-end gap-2">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault()
                  void send(input)
                }
              }}
              rows={2}
              placeholder="问筹码、共振、量价、撤单、竞价、妖股因子…（Enter 发送 / Shift+Enter 换行）"
              className="scrollbar min-h-[44px] flex-1 resize-none rounded-lg border border-border/60 bg-s0 px-2 py-1.5 text-[12px] outline-none focus:border-role-watch/60"
            />
            <button
              type="button"
              onClick={() => void send(input)}
              disabled={streaming || !input.trim()}
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
