import { useCallback, useEffect, useRef, useState } from 'react'
import { MessageCircle, X, Plus, Trash2, Send, ChevronLeft, XCircle, Settings2, Check, GripHorizontal, Newspaper, Paperclip, Loader2 } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { chatApi, fetchAPI, type ChatConversation, type ChatMessage } from '@panwatch/api'
import { useToast } from '@panwatch/base-ui/components/ui/toast'
import { parseServerTime } from '@/lib/utils'

interface StockContext {
  symbol: string
  market: string
  stockName: string
  pageContext?: string
}

/** 今日要闻数据源: /api/notifications 返回的单条通知(取 title + body 展示) */
interface ChatNewsItem {
  id: number
  title: string
  body?: string
  category?: string
  level?: string
  link?: string
  created_at?: string
}

type DesktopChatSize = 'compact' | 'standard' | 'large' | 'wide'
type DesktopChatPosition = 'left' | 'center' | 'right'

interface DesktopChatCoordinates {
  x: number
  y: number
}

interface DesktopChatDragState {
  pointerId: number
  offsetX: number
  offsetY: number
}

const CHAT_SIZE_STORAGE_KEY = 'panwatch_chat_desktop_size'
const CHAT_POSITION_STORAGE_KEY = 'panwatch_chat_desktop_position'
const CHAT_FREE_POSITION_STORAGE_KEY = 'panwatch_chat_desktop_free_position'
const CHAT_VIEWPORT_MARGIN = 12

/** 附件上传大小上限(与后端 /api/chat/upload 一致) */
const MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024
/** 附件上传允许的类型(与后端解析能力对齐) */
const ATTACHMENT_ACCEPT = 'image/png,image/jpeg,image/webp,.pdf,.xlsx,.xls,.csv,.txt,.md'

const DESKTOP_SIZE_OPTIONS: Array<{ value: DesktopChatSize; label: string; detail: string }> = [
  { value: 'compact', label: '紧凑', detail: '360 × 480' },
  { value: 'standard', label: '标准', detail: '420 × 600' },
  { value: 'large', label: '大窗口', detail: '560 × 720' },
  { value: 'wide', label: '宽屏', detail: '720 × 760' },
]

const DESKTOP_POSITION_OPTIONS: Array<{ value: DesktopChatPosition; label: string }> = [
  { value: 'left', label: '左下' },
  { value: 'center', label: '底部居中' },
  { value: 'right', label: '右下' },
]

const DESKTOP_SIZE_CLASSES: Record<DesktopChatSize, string> = {
  compact: 'md:w-[360px] md:h-[480px]',
  standard: 'md:w-[420px] md:h-[600px]',
  large: 'md:w-[560px] md:h-[720px]',
  wide: 'md:w-[720px] md:h-[760px]',
}

const DESKTOP_POSITION_CLASSES: Record<DesktopChatPosition, string> = {
  left: 'md:left-5 md:right-auto md:translate-x-0',
  center: 'md:left-1/2 md:right-auto md:-translate-x-1/2',
  right: 'md:left-auto md:right-5 md:translate-x-0',
}

const DESKTOP_FREE_POSITION_CLASSES = 'md:left-[var(--chat-x)] md:top-[var(--chat-y)] md:right-auto md:bottom-auto md:translate-x-0'

function readDesktopChatSize(): DesktopChatSize {
  if (typeof window === 'undefined') return 'standard'
  try {
    const value = window.localStorage.getItem(CHAT_SIZE_STORAGE_KEY)
    if (value === 'compact' || value === 'standard' || value === 'large' || value === 'wide') return value
  } catch {
    // localStorage may be unavailable in privacy-restricted browsers.
  }
  return 'standard'
}

function readDesktopChatPosition(): DesktopChatPosition {
  if (typeof window === 'undefined') return 'right'
  try {
    const value = window.localStorage.getItem(CHAT_POSITION_STORAGE_KEY)
    if (value === 'left' || value === 'center' || value === 'right') return value
  } catch {
    // localStorage may be unavailable in privacy-restricted browsers.
  }
  return 'right'
}

function readDesktopChatFreePosition(): DesktopChatCoordinates | null {
  if (typeof window === 'undefined') return null
  try {
    const rawValue = window.localStorage.getItem(CHAT_FREE_POSITION_STORAGE_KEY)
    if (!rawValue) return null
    const value = JSON.parse(rawValue) as Partial<DesktopChatCoordinates>
    if (Number.isFinite(value.x) && Number.isFinite(value.y)) {
      return { x: Number(value.x), y: Number(value.y) }
    }
  } catch {
    // Ignore invalid or unavailable persisted window coordinates.
  }
  return null
}

function clampCoordinate(value: number, minimum: number, maximum: number) {
  return Math.min(Math.max(value, minimum), maximum)
}

/**
 * 判断通知是否发生在 24 小时内(后端 created_at 为无时区 UTC, 补 Z 解析)。
 * 用于"今日要闻"过滤, 避免把历史遗留未读通知当成今日要闻展示。
 */
function isFreshNotification(createdAt?: string): boolean {
  if (!createdAt) return false
  const raw = /[zZ]$|[+-]\d{2}:\d{2}$/.test(createdAt) ? createdAt : `${createdAt}Z`
  const time = new Date(raw).getTime()
  if (!Number.isFinite(time)) return false
  return Date.now() - time < 24 * 60 * 60 * 1000
}

export default function ChatWidget() {
  const [open, setOpen] = useState(false)
  const [conversations, setConversations] = useState<ChatConversation[]>([])
  const [activeConvId, setActiveConvId] = useState<number | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [streamStage, setStreamStage] = useState<string | null>(null)
  const [view, setView] = useState<'list' | 'chat'>('list')
  const [stockContext, setStockContext] = useState<StockContext | null>(null)
  const [suggestedQuestions, setSuggestedQuestions] = useState<string[]>([])
  const [desktopSize, setDesktopSize] = useState<DesktopChatSize>(readDesktopChatSize)
  const [desktopPosition, setDesktopPosition] = useState<DesktopChatPosition>(readDesktopChatPosition)
  const [desktopDragPosition, setDesktopDragPosition] = useState<DesktopChatCoordinates | null>(readDesktopChatFreePosition)
  const [dragging, setDragging] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  // 今日要闻横条状态: newsItems=数据 / newsLoaded=是否已加载(避免闪烁) / newsDismissed=是否已收起
  const [newsItems, setNewsItems] = useState<ChatNewsItem[]>([])
  const [newsLoaded, setNewsLoaded] = useState(false)
  const [newsDismissed, setNewsDismissed] = useState(false)
  const endRef = useRef<HTMLDivElement>(null)
  const settingsRef = useRef<HTMLDivElement>(null)
  const chatWindowRef = useRef<HTMLDivElement>(null)
  const dragStateRef = useRef<DesktopChatDragState | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const { toast } = useToast()

  const loadConversations = useCallback(async () => {
    try {
      const list = await chatApi.listConversations(30)
      setConversations(list)
    } catch {
      // ignore
    }
  }, [])

  const loadMessages = useCallback(async (convId: number) => {
    try {
      const detail = await chatApi.getConversation(convId)
      setMessages(detail.messages)
    } catch {
      // ignore
    }
  }, [])

  const loadSuggestedQuestions = useCallback(async (symbol: string, market: string) => {
    try {
      const res = await chatApi.getSuggestedQuestions(symbol, market)
      setSuggestedQuestions(res.questions || [])
    } catch {
      setSuggestedQuestions([])
    }
  }, [])

  // Listen for stock context events from stock insight modal
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as StockContext
      if (!detail?.symbol) return
      setOpen(true)
      setStockContext(detail)
      setSuggestedQuestions([])
      setNewsDismissed(false)

      // Create a new conversation bound to this stock, with page context
      chatApi.createConversation({
        stock_symbol: detail.symbol,
        stock_market: detail.market,
        initial_context: detail.pageContext,
      }).then((conv) => {
        setActiveConvId(conv.id)
        setMessages([])
        setView('chat')
        setConversations((prev) => [conv, ...prev])
        loadSuggestedQuestions(detail.symbol, detail.market)
      }).catch(() => {
        // fallback: just open chat
        setView('chat')
      })
    }
    window.addEventListener('panwatch-open-chat', handler)
    return () => window.removeEventListener('panwatch-open-chat', handler)
  }, [loadSuggestedQuestions])

  useEffect(() => {
    if (open) {
      loadConversations()
    }
  }, [open, loadConversations])

  // 今日要闻: 对话打开且尚无消息时, 拉取系统最近未读通知(24 小时内, 最多 3 条); 无数据/失败则隐藏
  useEffect(() => {
    if (!open || view !== 'chat' || messages.length > 0 || newsDismissed) return
    let cancelled = false
    setNewsLoaded(false)
    fetchAPI<{ items: ChatNewsItem[] }>('/notifications?limit=10&only_unread=true', { cacheMode: 'reload' })
      .then((res) => {
        if (cancelled) return
        const fresh = (res?.items || [])
          .filter((item) => item?.title && isFreshNotification(item.created_at))
          .slice(0, 3)
        setNewsItems(fresh)
        setNewsLoaded(true)
      })
      .catch(() => {
        if (cancelled) return
        setNewsItems([])
        setNewsLoaded(true)
      })
    return () => {
      cancelled = true
    }
  }, [open, view, messages.length, newsDismissed])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  useEffect(() => {
    try {
      window.localStorage.setItem(CHAT_SIZE_STORAGE_KEY, desktopSize)
    } catch {
      // Keep the current session setting even when persistence is unavailable.
    }
  }, [desktopSize])

  useEffect(() => {
    try {
      window.localStorage.setItem(CHAT_POSITION_STORAGE_KEY, desktopPosition)
    } catch {
      // Keep the current session setting even when persistence is unavailable.
    }
  }, [desktopPosition])

  useEffect(() => {
    const timeoutId = window.setTimeout(() => {
      try {
        if (desktopDragPosition) {
          window.localStorage.setItem(CHAT_FREE_POSITION_STORAGE_KEY, JSON.stringify(desktopDragPosition))
        } else {
          window.localStorage.removeItem(CHAT_FREE_POSITION_STORAGE_KEY)
        }
      } catch {
        // Keep the current session position when persistence is unavailable.
      }
    }, 150)
    return () => window.clearTimeout(timeoutId)
  }, [desktopDragPosition])

  const hasDesktopDragPosition = desktopDragPosition !== null

  useEffect(() => {
    if (!open || !hasDesktopDragPosition) return

    const constrainToViewport = () => {
      if (window.innerWidth < 768) return
      const rect = chatWindowRef.current?.getBoundingClientRect()
      if (!rect) return

      setDesktopDragPosition((current) => {
        if (!current) return current
        const maximumX = Math.max(CHAT_VIEWPORT_MARGIN, window.innerWidth - rect.width - CHAT_VIEWPORT_MARGIN)
        const maximumY = Math.max(CHAT_VIEWPORT_MARGIN, window.innerHeight - rect.height - CHAT_VIEWPORT_MARGIN)
        const nextPosition = {
          x: clampCoordinate(current.x, CHAT_VIEWPORT_MARGIN, maximumX),
          y: clampCoordinate(current.y, CHAT_VIEWPORT_MARGIN, maximumY),
        }
        return nextPosition.x === current.x && nextPosition.y === current.y ? current : nextPosition
      })
    }

    const animationFrame = window.requestAnimationFrame(constrainToViewport)
    window.addEventListener('resize', constrainToViewport)
    return () => {
      window.cancelAnimationFrame(animationFrame)
      window.removeEventListener('resize', constrainToViewport)
    }
  }, [desktopSize, open, hasDesktopDragPosition])

  useEffect(() => {
    if (!settingsOpen) return

    const handlePointerDown = (event: MouseEvent) => {
      if (!settingsRef.current?.contains(event.target as Node)) setSettingsOpen(false)
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSettingsOpen(false)
    }

    document.addEventListener('mousedown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('mousedown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [settingsOpen])

  const handleDragStart = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (window.innerWidth < 768 || event.button !== 0) return
    if ((event.target as HTMLElement).closest('button, a, input, textarea, select, [role="button"]')) return

    const rect = chatWindowRef.current?.getBoundingClientRect()
    if (!rect) return

    dragStateRef.current = {
      pointerId: event.pointerId,
      offsetX: event.clientX - rect.left,
      offsetY: event.clientY - rect.top,
    }
    setDesktopDragPosition({ x: rect.left, y: rect.top })
    setDragging(true)
    event.currentTarget.setPointerCapture(event.pointerId)
    event.preventDefault()
  }, [])

  const handleDragMove = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    const dragState = dragStateRef.current
    if (!dragState || dragState.pointerId !== event.pointerId) return

    const rect = chatWindowRef.current?.getBoundingClientRect()
    if (!rect) return

    const maximumX = Math.max(CHAT_VIEWPORT_MARGIN, window.innerWidth - rect.width - CHAT_VIEWPORT_MARGIN)
    const maximumY = Math.max(CHAT_VIEWPORT_MARGIN, window.innerHeight - rect.height - CHAT_VIEWPORT_MARGIN)
    setDesktopDragPosition({
      x: clampCoordinate(event.clientX - dragState.offsetX, CHAT_VIEWPORT_MARGIN, maximumX),
      y: clampCoordinate(event.clientY - dragState.offsetY, CHAT_VIEWPORT_MARGIN, maximumY),
    })
    event.preventDefault()
  }, [])

  const handleDragEnd = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (dragStateRef.current?.pointerId !== event.pointerId) return
    dragStateRef.current = null
    setDragging(false)
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
  }, [])

  const openConversation = useCallback(async (conv: ChatConversation) => {
    setActiveConvId(conv.id)
    setView('chat')
    setSuggestedQuestions([])
    if (conv.stock_symbol && conv.stock_market) {
      setStockContext({ symbol: conv.stock_symbol, market: conv.stock_market, stockName: '' })
      loadSuggestedQuestions(conv.stock_symbol, conv.stock_market)
    } else {
      setStockContext(null)
    }
    await loadMessages(conv.id)
  }, [loadMessages, loadSuggestedQuestions])

  const createNewConversation = useCallback(async () => {
    try {
      const conv = await chatApi.createConversation()
      setActiveConvId(conv.id)
      setMessages([])
      setView('chat')
      setStockContext(null)
      setSuggestedQuestions([])
      setNewsDismissed(false)
      setConversations((prev) => [conv, ...prev])
    } catch {
      // ignore
    }
  }, [])

  const deleteConversation = useCallback(async (convId: number, e: React.MouseEvent) => {
    e.stopPropagation()
    try {
      await chatApi.deleteConversation(convId)
      setConversations((prev) => prev.filter((c) => c.id !== convId))
      if (activeConvId === convId) {
        setActiveConvId(null)
        setMessages([])
        setView('list')
        setStockContext(null)
        setSuggestedQuestions([])
      }
    } catch {
      // ignore
    }
  }, [activeConvId])

  const handleSend = useCallback(async (overrideContent?: string, imageData?: string) => {
    const content = (overrideContent || input).trim()
    if (!content || sending || uploading) return

    let convId = activeConvId
    if (!convId) {
      try {
        const conv = await chatApi.createConversation(
          stockContext ? { stock_symbol: stockContext.symbol, stock_market: stockContext.market } : undefined
        )
        convId = conv.id
        setActiveConvId(conv.id)
        setConversations((prev) => [conv, ...prev])
        setView('chat')
      } catch {
        return
      }
    }

    setInput('')
    setSending(true)
    setSuggestedQuestions([]) // hide after first send
    setStreamStage(null)

    const tempUserMsg: ChatMessage = {
      id: Date.now(),
      role: 'user',
      content,
      created_at: new Date().toISOString(),
    }
    setMessages((prev) => [...prev, tempUserMsg])

    // 流式: 先放一个空的 assistant 气泡, 由 SSE 事件逐步填充(阶段提示 + 打字机正文)
    const tempAssistantId = Date.now() + 1
    const placeholderAssistantMsg: ChatMessage = {
      id: tempAssistantId,
      role: 'assistant',
      content: '',
      created_at: new Date().toISOString(),
    }
    setMessages((prev) => [...prev, placeholderAssistantMsg])

    let streamedContent = ''
    try {
      await chatApi.sendMessageStream(
        convId,
        content,
        {
          onStage: (msg) => setStreamStage(msg),
          onDelta: (chunk) => {
            streamedContent += chunk
            setMessages((prev) =>
              prev.map((m) =>
                m.id === tempAssistantId ? { ...m, content: streamedContent } : m
              )
            )
          },
          onDone: (msg) => {
            setMessages((prev) => prev.map((m) => (m.id === tempAssistantId ? msg : m)))
          },
          onError: (errMsg) => {
            setMessages((prev) =>
              prev.map((m) => (m.id === tempAssistantId ? { ...m, content: errMsg } : m))
            )
          },
        },
        undefined,
        imageData
      )
      setConversations((prev) =>
        prev.map((c) => c.id === convId ? { ...c, title: c.title || content.slice(0, 20) } : c)
      )
    } catch (e) {
      // 流中断/网络错误: 气泡里已有内容则保留, 否则显示错误
      const errMsg = `请求失败：${e instanceof Error ? e.message : '未知错误'}`
      setMessages((prev) =>
        prev.map((m) => (m.id === tempAssistantId ? { ...m, content: m.content || errMsg } : m))
      )
    } finally {
      setSending(false)
      setStreamStage(null)
    }
  }, [input, sending, uploading, activeConvId, stockContext])

  /**
   * 选择附件: 先 POST /api/chat/upload 解析(multipart form-data), 拿到 text 后
   * 组装成「用户原话 + [附件: 文件名] + 解析文本/错误提示」作为消息发送。
   */
  const handleAttachmentSelect = useCallback(async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = '' // 允许重复选择同一文件
    if (!file) return

    if (file.size > MAX_ATTACHMENT_SIZE) {
      toast(`附件超过 20MB 限制: ${file.name}`, 'error')
      return
    }

    setUploading(true)
    try {
      const res = await chatApi.uploadAttachment(file)
      const filename = res.filename || file.name
      const parsedText = (res.text || '').trim()
      const parts: string[] = []
      const userWords = input.trim()
      if (userWords) parts.push(userWords)
      parts.push(`[附件: ${filename}]`)
      parts.push(parsedText ? parsedText : (res.error ? `附件解析失败: ${res.error}` : '附件未解析出内容'))
      setInput('')
      setUploading(false) // 解析阶段结束, 放行 handleSend(其内部以 sending 防重入)
      await handleSend(parts.join('\n\n'), res.image_data || undefined)
    } catch (e) {
      const message = e instanceof Error ? e.message : '未知错误'
      toast(`附件上传失败: ${message}`, 'error')
    } finally {
      setUploading(false)
    }
  }, [input, handleSend, toast])

  // 点击今日要闻: 把该条内容作为用户消息自动提问, 并收起横条; 顺手标记已读避免下次重复出现
  const handleNewsClick = useCallback((item: ChatNewsItem) => {
    setNewsDismissed(true)
    setNewsItems((prev) => prev.filter((n) => n.id !== item.id))
    fetchAPI(`/notifications/${item.id}/read`, { method: 'POST' }).catch(() => {
      // 标记已读失败不影响提问
    })
    const question = `系统通知「${item.title}」${item.body ? `：${item.body}` : ''}。这条提醒具体什么情况？需要关注吗？`
    handleSend(question)
  }, [handleSend])

  const desktopWindowPositionClasses = desktopDragPosition
    ? DESKTOP_FREE_POSITION_CLASSES
    : `md:bottom-5 ${DESKTOP_POSITION_CLASSES[desktopPosition]}`
  const desktopWindowStyle = desktopDragPosition
    ? ({
        '--chat-x': `${desktopDragPosition.x}px`,
        '--chat-y': `${desktopDragPosition.y}px`,
      } as React.CSSProperties)
    : undefined

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className={`fixed bottom-20 right-4 md:bottom-5 z-40 w-12 h-12 rounded-full bg-primary text-primary-foreground shadow-lg flex items-center justify-center hover:bg-primary/90 transition-colors ${DESKTOP_POSITION_CLASSES[desktopPosition]}`}
        title="打开数智分析BOT"
        aria-label="打开数智分析BOT"
      >
        <MessageCircle className="w-5 h-5" />
      </button>
    )
  }

  return (
      <div
        ref={chatWindowRef}
        style={desktopWindowStyle}
        className={`fixed bottom-0 right-0 z-[60] w-full h-full md:max-w-[calc(100vw-1.5rem)] md:max-h-[calc(100vh-1.5rem)] md:rounded-xl bg-background border border-border/60 shadow-2xl md:border-primary/30 md:ring-1 md:ring-white/10 md:shadow-[0_24px_72px_rgba(0,0,0,0.58),0_0_30px_rgba(79,70,229,0.16)] flex flex-col overflow-hidden ${DESKTOP_SIZE_CLASSES[desktopSize]} ${desktopWindowPositionClasses}`}
      >
      {/* Header */}
      <div
        onPointerDown={handleDragStart}
        onPointerMove={handleDragMove}
        onPointerUp={handleDragEnd}
        onPointerCancel={handleDragEnd}
        className={`flex items-center justify-between px-4 py-3 border-b border-border/40 bg-accent/20 md:touch-none ${dragging ? 'md:cursor-grabbing md:select-none' : 'md:cursor-grab'}`}
        title="拖动标题栏移动窗口"
      >
        <div className="flex items-center gap-2">
          <GripHorizontal className="hidden h-4 w-4 shrink-0 text-muted-foreground/50 md:block" aria-hidden="true" />
          {view === 'chat' && (
            <button
              onClick={() => { setView('list'); setStockContext(null); setSuggestedQuestions([]); loadConversations() }}
              className="text-muted-foreground hover:text-foreground transition-colors"
            >
              <ChevronLeft className="w-4 h-4" />
            </button>
          )}
          <span className="text-[14px] font-semibold text-foreground">AI 助手</span>
          {view === 'chat' && stockContext && (
            <span className="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full bg-primary/10 text-primary">
              {stockContext.market}:{stockContext.symbol}
              {stockContext.stockName && ` ${stockContext.stockName}`}
              <button
                onClick={() => { setStockContext(null); setSuggestedQuestions([]) }}
                className="hover:text-primary/70 transition-colors"
              >
                <XCircle className="w-3 h-3" />
              </button>
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          {view === 'list' && (
            <button
              onClick={createNewConversation}
              className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-accent/50 transition-colors"
              title="新建对话"
            >
              <Plus className="w-4 h-4" />
            </button>
          )}
          <div ref={settingsRef} className="relative hidden md:block">
            <button
              onClick={() => setSettingsOpen((current) => !current)}
              className={`p-1.5 rounded-md transition-colors ${settingsOpen ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground hover:bg-accent/50'}`}
              title="窗口设置"
              aria-label="窗口设置"
              aria-expanded={settingsOpen}
            >
              <Settings2 className="w-4 h-4" />
            </button>
            {settingsOpen && (
              <div className="absolute right-0 top-9 z-20 w-[280px] rounded-xl border border-border/70 bg-background p-3 shadow-2xl">
                <div className="mb-2 text-[11px] font-medium text-muted-foreground">窗口大小</div>
                <div className="grid grid-cols-2 gap-1.5">
                  {DESKTOP_SIZE_OPTIONS.map((option) => {
                    const active = desktopSize === option.value
                    return (
                      <button
                        key={option.value}
                        onClick={() => setDesktopSize(option.value)}
                        className={`flex min-w-0 items-center justify-between rounded-lg border px-2.5 py-2 text-left transition-colors ${active ? 'border-primary/60 bg-primary/10 text-primary' : 'border-border/50 bg-accent/20 text-foreground hover:bg-accent/50'}`}
                      >
                        <span className="min-w-0">
                          <span className="block text-[12px] font-medium">{option.label}</span>
                          <span className="block text-[10px] text-muted-foreground">{option.detail}</span>
                        </span>
                        {active && <Check className="ml-1 h-3.5 w-3.5 shrink-0" />}
                      </button>
                    )
                  })}
                </div>

                <div className="mb-2 mt-3 flex items-center justify-between text-[11px] font-medium text-muted-foreground">
                  <span>停靠位置</span>
                  {desktopDragPosition && <span className="rounded-full bg-primary/10 px-1.5 py-0.5 text-[9px] text-primary">自由位置</span>}
                </div>
                <div className="grid grid-cols-3 gap-1.5">
                  {DESKTOP_POSITION_OPTIONS.map((option) => {
                    const active = !desktopDragPosition && desktopPosition === option.value
                    return (
                      <button
                        key={option.value}
                        onClick={() => { setDesktopPosition(option.value); setDesktopDragPosition(null) }}
                        className={`flex items-center justify-center gap-1 rounded-lg border px-2 py-2 text-[11px] transition-colors ${active ? 'border-primary/60 bg-primary/10 text-primary' : 'border-border/50 bg-accent/20 text-foreground hover:bg-accent/50'}`}
                      >
                        {active && <Check className="h-3 w-3 shrink-0" />}
                        {option.label}
                      </button>
                    )
                  })}
                </div>
                <p className="mt-2.5 text-[10px] leading-relaxed text-muted-foreground">拖动标题栏可自由定位；选择停靠位置可复位。设置会保存在当前浏览器。</p>
              </div>
            )}
          </div>
          <button
            onClick={() => { setSettingsOpen(false); setOpen(false) }}
            className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-accent/50 transition-colors"
            aria-label="关闭 AI 助手"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* 今日要闻横条: 对话打开且无消息时, 顶部展示系统最近通知/异动(最多3条); 无数据隐藏; 点击即发送提问 */}
      {view === 'chat' && newsLoaded && !newsDismissed && messages.length === 0 && !sending && newsItems.length > 0 && (
        <div className="shrink-0 px-3 pt-2 pb-2 border-b border-border/30 bg-accent/10">
          <div className="mb-1.5 flex items-center justify-between">
            <span className="inline-flex items-center gap-1 text-[11px] font-medium text-foreground">
              <Newspaper className="h-3.5 w-3.5 text-primary" />
              今日要闻
            </span>
            <button
              onClick={() => setNewsDismissed(true)}
              className="text-[10px] text-muted-foreground/70 transition-colors hover:text-muted-foreground"
              title="收起今日要闻"
            >
              收起
            </button>
          </div>
          <div className="flex flex-col gap-1.5">
            {newsItems.map((item) => (
              <button
                key={item.id}
                onClick={() => handleNewsClick(item)}
                disabled={sending}
                className="rounded-lg border border-border/50 bg-background/70 px-2.5 py-1.5 text-left transition-colors hover:border-primary/40 hover:bg-primary/5"
              >
                <div className="truncate text-[12px] text-foreground">{item.title}</div>
                {item.body && (
                  <div className="mt-0.5 line-clamp-2 text-[11px] leading-snug text-muted-foreground">{item.body}</div>
                )}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* List view */}
      {view === 'list' && (
        <div className="flex-1 overflow-y-auto scrollbar">
          {conversations.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full text-muted-foreground text-[13px] gap-3">
              <MessageCircle className="w-8 h-8 opacity-30" />
              <p>暂无对话</p>
              <button
                onClick={createNewConversation}
                className="text-[12px] px-4 py-2 rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
              >
                开始新对话
              </button>
            </div>
          ) : (
            conversations.map((conv) => (
              <button
                key={conv.id}
                onClick={() => openConversation(conv)}
                className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-accent/30 transition-colors border-b border-border/20"
              >
                <div className="min-w-0 flex-1">
                  <div className="text-[13px] text-foreground truncate">
                    {conv.title || '新对话'}
                  </div>
                  <div className="text-[11px] text-muted-foreground mt-0.5">
                    {conv.stock_symbol ? `${conv.stock_market}:${conv.stock_symbol} · ` : ''}
                    {parseServerTime(conv.created_at).toLocaleDateString()}
                  </div>
                </div>
                <button
                  onClick={(e) => deleteConversation(conv.id, e)}
                  className="p-1 rounded text-muted-foreground/50 hover:text-rose-700 dark:hover:text-rose-400 transition-colors shrink-0"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </button>
            ))
          )}
        </div>
      )}

      {/* Chat view */}
      {view === 'chat' && (
        <>
          <div className="flex-1 overflow-y-auto scrollbar px-4 py-3 space-y-3">
            {/* Suggested questions */}
            {messages.length === 0 && suggestedQuestions.length > 0 && (
              <div className="flex flex-col gap-2">
                <span className="text-[11px] text-muted-foreground">推荐问题</span>
                <div className="flex flex-wrap gap-2">
                  {suggestedQuestions.map((q) => (
                    <button
                      key={q}
                      className="text-[11px] px-3 py-1.5 rounded-full bg-primary/10 text-primary hover:bg-primary/20 transition-colors text-left"
                      onClick={() => handleSend(q)}
                      disabled={sending}
                    >
                      {q}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {messages.length === 0 && suggestedQuestions.length === 0 && !sending && (
              <div className="flex flex-col items-center justify-center h-full text-muted-foreground text-[13px] gap-2">
                <MessageCircle className="w-6 h-6 opacity-30" />
                <p>输入问题开始对话</p>
              </div>
            )}
            {/* 流式占位气泡内容为空时隐藏, 由下方阶段提示条承载等待反馈 */}
            {messages.filter((msg) => !(msg.role === 'assistant' && !msg.content)).map((msg) => (
              <div
                key={msg.id}
                className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
              >
                <div
                  className={`min-w-0 max-w-[85%] rounded-xl px-3 py-2 text-[13px] leading-relaxed ${
                    msg.role === 'user'
                      ? 'bg-primary text-primary-foreground'
                      : 'bg-accent/60 text-foreground'
                  }`}
                >
                  {msg.role === 'assistant' ? (
                    <div className="prose prose-sm dark:prose-invert max-w-none min-w-0 break-words [&_p]:my-1 [&_ul]:my-1 [&_ol]:my-1 [&_li]:my-0.5 [&_h1]:text-[15px] [&_h2]:text-[14px] [&_h3]:text-[13px] [&_pre]:max-w-full [&_pre]:overflow-x-auto [&_pre]:text-[11px] [&_code]:break-words">
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm]}
                        components={{
                          table: ({ children }) => (
                            <div className="my-2 max-w-full overflow-x-auto rounded-lg border border-border/60">
                              <table className="m-0 w-max min-w-full border-collapse text-[11px]">{children}</table>
                            </div>
                          ),
                          th: ({ children }) => (
                            <th className="whitespace-nowrap border-b border-r border-border/60 bg-background/60 px-2 py-1.5 text-left font-semibold last:border-r-0">
                              {children}
                            </th>
                          ),
                          td: ({ children }) => (
                            <td className="min-w-[88px] border-b border-r border-border/40 px-2 py-1.5 align-top last:border-r-0">
                              {children}
                            </td>
                          ),
                          a: ({ children, href }) => (
                            <a href={href} target="_blank" rel="noopener noreferrer" className="break-all text-primary underline underline-offset-2">
                              {children}
                            </a>
                          ),
                        }}
                      >
                        {msg.content}
                      </ReactMarkdown>
                    </div>
                  ) : (
                    msg.content
                  )}
                </div>
              </div>
            ))}
            {sending && (
              <div className="flex justify-start">
                <div className="bg-accent/60 rounded-xl px-3 py-2 text-[13px] text-muted-foreground flex items-center gap-2">
                  <span className="w-3 h-3 border-2 border-current/30 border-t-current rounded-full animate-spin" />
                  {streamStage || '思考中...'}
                </div>
              </div>
            )}
            <div ref={endRef} />
          </div>

          {/* Input */}
          <div className="flex items-center gap-2 px-4 py-3 border-t border-border/40">
            <input
              ref={fileInputRef}
              type="file"
              accept={ATTACHMENT_ACCEPT}
              onChange={handleAttachmentSelect}
              className="hidden"
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={sending || uploading}
              className="h-9 w-9 shrink-0 rounded-lg text-muted-foreground hover:text-foreground hover:bg-accent/50 transition-colors disabled:opacity-40 flex items-center justify-center"
              title="上传图片/文件(图片OCR / Excel / PDF / txt)"
              aria-label="上传图片或文件"
            >
              {uploading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Paperclip className="w-4 h-4" />}
            </button>
            <input
              type="text"
              className="flex-1 h-9 px-3 rounded-lg bg-accent/40 text-[13px] text-foreground placeholder:text-muted-foreground outline-none focus:ring-1 focus:ring-primary/30"
              placeholder={uploading ? '正在解析附件...' : '输入问题...'}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault()
                  handleSend()
                }
              }}
              disabled={sending || uploading}
            />
            {uploading && (
              <span className="shrink-0 text-[11px] text-muted-foreground flex items-center gap-1 whitespace-nowrap">
                <Loader2 className="w-3 h-3 animate-spin" />
                解析中...
              </span>
            )}
            <button
              className="h-9 w-9 rounded-lg bg-primary text-primary-foreground flex items-center justify-center hover:bg-primary/90 transition-colors disabled:opacity-50"
              onClick={() => handleSend()}
              disabled={sending || uploading || !input.trim()}
            >
              <Send className="w-4 h-4" />
            </button>
          </div>
        </>
      )}
      </div>
  )
}
