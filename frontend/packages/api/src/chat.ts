import { API_BASE, fetchAPI, getToken, logout } from './client'

export interface ChatConversation {
  id: number
  title: string
  stock_symbol?: string | null
  stock_market?: string | null
  created_at: string
}

/** 依据行数字对账（服务端 `src/core/evidence_row_audit.py` 产出；只报"数字在不在本轮取数里"）。 */
export interface EvidenceRowAudit {
  basis: 'number_containment'
  rows: Array<{ index: number; text: string; claims: string[]; unverified: string[] }>
  checked: number
  unverified_total: number
  /** 没有依据段/没有数字时的原因；正常为空串。 */
  reason: string
}

export interface ChatMessage {
  id: number
  role: 'user' | 'assistant' | 'system'
  content: string
  run_id?: string | null
  /** 旧消息为 null（当时没有取数原文，无法重算，不补）。 */
  evidence_audit?: EvidenceRowAudit | null
  created_at: string
}

export interface ConversationDetail {
  conversation: ChatConversation
  messages: ChatMessage[]
}

/**
 * 2026-10-28 SSE 终稿/坏帧可见化(P0)。
 *
 * 契约真源 `src/web/api/chat.py::send_message_stream`: `stage` / `delta` / `done` / `error`
 * 四种事件, 空行分隔。**`done.content` 是落库全文, 与 delta 拼接不等价**:
 *   - 首轮「问具体标的却零工具作答」时服务端用零工具拒答替换正文 → 一个 delta 都没发;
 *   - 数字对账附言只加在结尾全文里, 不再补发增量;
 *   - 流式异常时服务端以 done 收尾一句失败文案;
 *   - 客户端断流(HTTP 200 后流提前结束)则**根本没有 done**。
 * 所以前端必须: 终稿覆盖增量; 没终稿时当失败(不伪称成功); 坏帧计数可见。
 *
 * 帧解析抽成不依赖 fetch 的纯逻辑, 端点与单测共用同一份(单测不许另写解析器冒充证据)。
 */
export type ChatStreamEvent =
  | { type: 'stage'; message: string }
  | { type: 'delta'; content: string }
  | { type: 'done'; message: ChatMessage }
  | { type: 'error'; message: string }
  | { type: 'malformed'; reason: string; raw: string }
  | { type: 'ignored' }

/** 空行分隔帧; 兼容代理改写出的 CRLF(旧实现只找 `\n\n`, CRLF 会把整轮流当成一帧)。 */
const FRAME_SEP = /\r?\n\r?\n/

/** 解析单个 SSE 帧。坏帧返回 `malformed`(带原因与原文), 绝不静默丢内容。 */
export function parseChatStreamFrame(raw: string): ChatStreamEvent {
  let event = 'message'
  const dataLines: string[] = []
  for (const rawLine of raw.split('\n')) {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
    if (!line || line.startsWith(':')) continue // 注释 / keep-alive
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''))
  }
  if (!dataLines.length) return { type: 'ignored' }
  let payload: any
  try {
    payload = JSON.parse(dataLines.join('\n'))
  } catch {
    return { type: 'malformed', reason: 'invalid-json', raw }
  }
  if (!payload || typeof payload !== 'object') return { type: 'malformed', reason: 'invalid-payload', raw }
  switch (event) {
    case 'stage':
      return typeof payload.message === 'string'
        ? { type: 'stage', message: payload.message }
        : { type: 'malformed', reason: 'stage-shape', raw }
    case 'delta':
      // 旧实现 `payload.content || ''` 会把缺字段的坏帧变成静默空增量
      return typeof payload.content === 'string'
        ? { type: 'delta', content: payload.content }
        : { type: 'malformed', reason: 'delta-shape', raw }
    case 'done':
      return typeof payload.content === 'string'
        ? { type: 'done', message: payload as ChatMessage }
        : { type: 'malformed', reason: 'done-shape', raw }
    case 'error':
      return typeof payload.message === 'string'
        ? { type: 'error', message: payload.message }
        : { type: 'malformed', reason: 'error-shape', raw }
    default:
      // 未知事件 = 服务端将来新增的类型, 不当作损坏(但也不猜它的语义)
      return { type: 'ignored' }
  }
}

export interface ChatStreamSession {
  /** 灌入一块(可能切断帧; 多字节字符由 TextDecoder 负责拼回)文本, 返回可派发的事件。 */
  feed(chunk: string): ChatStreamEvent[]
  /** 流结束: 派发 buffer 里最后一帧(收尾没有空行也合法), 并清空。 */
  end(): ChatStreamEvent[]
  /** 已收 delta 拼起来的正文(非权威, 仅供流式显示/断流兜底)。 */
  readonly text: string
  /** 服务端终稿(done 帧), 未收到为 null。 */
  readonly final: ChatMessage | null
  /** error 帧原文(按到达顺序)。 */
  readonly errors: string[]
  /** 坏帧数量(>0 表示有帧内容没进正文)。 */
  readonly corruptedFrames: number
}

/** 单次流的解析状态机。**每次流都要新建**(buffer/text 不跨流共享 → 并发不串)。 */
export function createChatStreamSession(): ChatStreamSession {
  let buffer = ''
  let text = ''
  let final: ChatMessage | null = null
  const errors: string[] = []
  let corruptedFrames = 0

  const absorb = (raw: string): ChatStreamEvent => {
    const event = parseChatStreamFrame(raw)
    if (event.type === 'delta') text += event.content
    else if (event.type === 'done') final = event.message
    else if (event.type === 'error') errors.push(event.message)
    else if (event.type === 'malformed') corruptedFrames += 1
    return event
  }

  const drain = (flush: boolean): ChatStreamEvent[] => {
    const out: ChatStreamEvent[] = []
    for (;;) {
      const hit = FRAME_SEP.exec(buffer)
      if (!hit) break
      const raw = buffer.slice(0, hit.index)
      buffer = buffer.slice(hit.index + hit[0].length)
      if (raw.trim()) out.push(absorb(raw))
    }
    if (flush) {
      if (buffer.trim()) out.push(absorb(buffer))
      buffer = ''
    }
    return out
  }

  return {
    feed: (chunk) => {
      buffer += chunk
      return drain(false)
    },
    end: () => drain(true),
    get text() {
      return text
    },
    get final() {
      return final
    },
    get errors() {
      return errors
    },
    get corruptedFrames() {
      return corruptedFrames
    },
  }
}

/** 2026-08-13 流式聊天回调: stage=阶段提示 / delta=正文增量 / done=终稿(落库全文) / error=流错误 */
export interface ChatStreamHandlers {
  onStage?: (message: string) => void
  onDelta?: (content: string) => void
  onDone?: (message: ChatMessage) => void
  onError?: (message: string) => void
  /** 坏帧(JSON 截断/字段缺失): 内容没进正文, 调用方要让它可见而不是装作没发生。 */
  onMalformed?: (info: { reason: string; raw: string }) => void
}

/** 一轮流的结论。`authoritative=false` 或 `failure` 有值都不得被当成功渲染。 */
export interface ChatStreamResult {
  /** 终稿正文: 拿到 done 且非空 = 服务端落库全文(权威); 否则是已收 delta 拼接(非权威)。 */
  content: string
  /** done 是否到达且可用(终稿权威)。 */
  authoritative: boolean
  /** 坏帧数量。 */
  corruptedFrames: number
  /** 服务端 error 帧文案。 */
  errors: string[]
  /** 无法安全继续的原因: 有值即"不能当成功"的可见失败文案。 */
  failure?: string
}

function deliver(event: ChatStreamEvent, handlers: ChatStreamHandlers) {
  switch (event.type) {
    case 'stage':
      handlers.onStage?.(event.message)
      break
    case 'delta':
      handlers.onDelta?.(event.content)
      break
    case 'done':
      handlers.onDone?.(event.message)
      break
    case 'error':
      handlers.onError?.(event.message)
      break
    case 'malformed':
      handlers.onMalformed?.({ reason: event.reason, raw: event.raw })
      break
    default:
      break
  }
}

export const chatApi = {
  createConversation: (params?: { stock_symbol?: string; stock_market?: string; initial_context?: string }) =>
    fetchAPI<ChatConversation>('/chat/conversations', {
      method: 'POST',
      body: JSON.stringify(params || {}),
    }),

  listConversations: (limit = 30) =>
    fetchAPI<ChatConversation[]>(`/chat/conversations?limit=${limit}`),

  getConversation: (id: number) =>
    fetchAPI<ConversationDetail>(`/chat/conversations/${id}`),

  deleteConversation: (id: number) =>
    fetchAPI<{ ok: boolean }>(`/chat/conversations/${id}`, {
      method: 'DELETE',
    }),

  sendMessage: (conversationId: number, content: string, imageData?: string) =>
    fetchAPI<ChatMessage>(`/chat/conversations/${conversationId}/messages`, {
      method: 'POST',
      body: JSON.stringify({ content, image_data: imageData || undefined }),
      timeoutMs: 120000,
    }),

  /** 2026-08-13 流式发送: fetch + ReadableStream 解析 SSE, 消除 10-60s 白屏等待。 */
  sendMessageStream: (
    conversationId: number,
    content: string,
    handlers: ChatStreamHandlers,
    signal?: AbortSignal,
    imageData?: string,
    stock?: { symbol: string; market?: string },
    mentions?: { type: string; value: string; label?: string }[],
  ): Promise<ChatStreamResult> =>
    new Promise<ChatStreamResult>((resolve, reject) => {
      fetch(`${API_BASE}/chat/conversations/${conversationId}/messages/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
        },
        body: JSON.stringify({
          content,
          image_data: imageData || undefined,
          // 当前查看的标的：让后端把对话绑到这只票，避免模型从持仓里"猜"成别的股票
          stock_symbol: stock?.symbol || undefined,
          stock_market: stock?.market || undefined,
          mentions: mentions?.length ? mentions : undefined,
        }),
        signal,
      })
        .then(async (res) => {
          if (res.status === 401) {
            logout()
            throw new Error('登录已过期')
          }
          if (!res.ok || !res.body) {
            throw new Error(`HTTP ${res.status}`)
          }
          const reader = res.body.getReader()
          const decoder = new TextDecoder('utf-8')
          const session = createChatStreamSession()
          const emit = (events: ChatStreamEvent[]) => {
            for (const event of events) deliver(event, handlers)
          }
          for (;;) {
            const { value, done } = await reader.read()
            if (done) break
            // stream:true: 多字节字符被切断时由 decoder 自己拼回
            emit(session.feed(decoder.decode(value, { stream: true })))
          }
          emit(session.feed(decoder.decode()))
          emit(session.end())

          const finalMessage = session.final
          const finalText = finalMessage?.content ?? ''
          const result: ChatStreamResult = {
            content: session.text,
            authoritative: false,
            corruptedFrames: session.corruptedFrames,
            errors: [...session.errors],
          }
          if (finalMessage && finalText !== '') {
            // 终稿权威: 覆盖增量(对账附言/拒答替换/失败文案都只在这里)
            result.content = finalText
            result.authoritative = true
          } else if (finalMessage) {
            // 终稿是空串: 不拿它把已经流出的正文抹白, 但也绝不算成功
            result.failure = '服务端终稿为空，本次回答可能不完整'
          } else if (session.errors.length) {
            result.failure = session.errors[0]
          } else if (session.corruptedFrames > 0) {
            result.failure = `流式数据损坏（${session.corruptedFrames} 处），且未收到服务端终稿，回答可能不完整`
          } else {
            result.failure = '流式响应未收到服务端终稿，回答可能不完整'
          }
          return result
        })
        .then(resolve, (err: any) => {
          if (err?.name === 'AbortError') {
            reject(new Error('请求已中断'))
          } else {
            reject(err)
          }
        })
    }),

  getSuggestedQuestions: (symbol: string, market: string) =>
    fetchAPI<{ questions: string[] }>(
      `/chat/suggested-questions?symbol=${encodeURIComponent(symbol)}&market=${encodeURIComponent(market)}`
    ),

  /** 2026-08-14 附件上传/解析: multipart form-data, 返回 {text, filename, image_data?, error?} */
  uploadAttachment: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return fetchAPI<{ text: string; filename: string; image_data?: string; error?: string }>(
      '/chat/upload',
      {
        method: 'POST',
        body: form,
        timeoutMs: 120000,
      },
    )
  },
}
