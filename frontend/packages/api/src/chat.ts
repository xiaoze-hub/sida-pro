import { API_BASE, fetchAPI, getToken, logout } from './client'

export interface ChatConversation {
  id: number
  title: string
  stock_symbol?: string | null
  stock_market?: string | null
  created_at: string
}

export interface ChatMessage {
  id: number
  role: 'user' | 'assistant' | 'system'
  content: string
  created_at: string
}

export interface ConversationDetail {
  conversation: ChatConversation
  messages: ChatMessage[]
}

/** 2026-08-13 流式聊天回调: stage=阶段提示 / delta=正文增量(打字机) / done=落库完成 / error=流错误 */
export interface ChatStreamHandlers {
  onStage?: (message: string) => void
  onDelta?: (content: string) => void
  onDone?: (message: ChatMessage) => void
  onError?: (message: string) => void
}

function dispatchSSEEvent(rawEvent: string, handlers: ChatStreamHandlers) {
  const lines = rawEvent.replace(/\r/g, '').split('\n')
  let event = 'message'
  const dataLines: string[] = []
  for (const line of lines) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
  }
  if (dataLines.length === 0) return
  let payload: any
  try {
    payload = JSON.parse(dataLines.join('\n'))
  } catch {
    return
  }
  switch (event) {
    case 'stage':
      handlers.onStage?.(payload.message || '')
      break
    case 'delta':
      handlers.onDelta?.(payload.content || '')
      break
    case 'done':
      handlers.onDone?.(payload as ChatMessage)
      break
    case 'error':
      handlers.onError?.(payload.message || '请求失败')
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
  ): Promise<void> =>
    new Promise<void>((resolve, reject) => {
      fetch(`${API_BASE}/chat/conversations/${conversationId}/messages/stream`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
        },
        body: JSON.stringify({ content, image_data: imageData || undefined }),
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
          let buffer = ''
          let done = false
          while (!done) {
            const { value, done: streamDone } = await reader.read()
            done = streamDone
            buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
            // SSE 事件以空行分隔, 逐条派发(半条留在 buffer 等下一块)
            let sepIndex = buffer.indexOf('\n\n')
            while (sepIndex !== -1) {
              const rawEvent = buffer.slice(0, sepIndex)
              buffer = buffer.slice(sepIndex + 2)
              dispatchSSEEvent(rawEvent, handlers)
              sepIndex = buffer.indexOf('\n\n')
            }
          }
          if (buffer.trim()) {
            dispatchSSEEvent(buffer, handlers)
          }
          resolve()
        })
        .catch((err: any) => {
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
