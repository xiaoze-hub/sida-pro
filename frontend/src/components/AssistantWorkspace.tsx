import { createContext, Suspense, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useLocation, useNavigate } from 'react-router-dom'
import { TabActiveProvider, useTabActive } from '@panwatch/base-ui'
import { lazyWithRetry } from '@/lib/lazy-with-retry'
import ErrorBoundary from '@/components/ErrorBoundary'
import { AssistantActivityContext } from '@/lib/assistant-avatar'

const Conversation = lazyWithRetry(() => import('@/pages/AiCopilot'), 'assistant-conversation')
export interface AssistantEntry { id: number; symbol: string; market?: string; pageContext?: string }
interface AssistantController {
  open: boolean; setOpen: (open: boolean) => void; busy: boolean; enabled: boolean; pageActive: boolean
  registerPage: (node: HTMLElement | null) => void; registerMini: (node: HTMLElement | null) => void
  expand: () => void; configure: () => void
}
const Context = createContext<AssistantController | null>(null)
export const useAssistantWorkspace = () => useContext(Context)

/** 一个会话实例、一个稳定的 portal 容器；仅切换承载位置，不重建对话/流。 */
export function AssistantWorkspaceProvider({ children, enabled = true }: { children: ReactNode; enabled?: boolean }) {
  const location = useLocation()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [pageHost, setPageHost] = useState<HTMLElement | null>(null)
  const [miniHost, setMiniHost] = useState<HTMLElement | null>(null)
  const [started, setStarted] = useState(false)
  const [entry, setEntry] = useState<AssistantEntry | null>(null)
  const entrySequence = useRef(0)
  const parking = useRef<HTMLDivElement>(null)
  const [container] = useState(() => {
    const node = document.createElement('div')
    node.className = 'h-full min-h-0'
    node.dataset.assistantInstance = 'shared'
    return node
  })
  const registerPage = useCallback((node: HTMLElement | null) => setPageHost(node), [])
  const registerMini = useCallback((node: HTMLElement | null) => setMiniHost(node), [])
  const visible = enabled && (!!pageHost || open)

  useEffect(() => { if (visible) setStarted(true) }, [visible])
  useEffect(() => { if (pageHost) setOpen(false) }, [pageHost])
  useLayoutEffect(() => {
    const target = pageHost || (open ? miniHost : null) || parking.current
    if (target && container.parentElement !== target) target.appendChild(container)
  }, [container, pageHost, miniHost, open])
  useEffect(() => {
    const handleEntry = (event: Event) => {
      if (!enabled) return
      const detail = (event as CustomEvent<Partial<AssistantEntry>>).detail
      if (!detail?.symbol) return
      setEntry({ id: ++entrySequence.current, symbol: detail.symbol, market: detail.market, pageContext: detail.pageContext })
      setOpen(true)
    }
    window.addEventListener('panwatch-open-chat', handleEntry)
    return () => window.removeEventListener('panwatch-open-chat', handleEntry)
  }, [enabled])
  const expand = useCallback(() => { setOpen(false); navigate('/ai-copilot') }, [navigate])
  const configure = useCallback(() => { setOpen(false); navigate('/settings?tab=settings&section=sec-appearance') }, [navigate])
  const value = useMemo(() => ({ open, setOpen, busy, enabled, pageActive: !!pageHost, registerPage, registerMini, expand, configure }), [open, busy, enabled, pageHost, registerPage, registerMini, expand, configure])
  return <Context.Provider value={value}><AssistantActivityContext.Provider value={busy}>
    {children}
    <div ref={parking} hidden aria-hidden="true" />
    {started && enabled && createPortal(<TabActiveProvider active={visible}>
      <ErrorBoundary fallback={(_error, retry) => <div className="p-4 text-[14px]">助手暂时无法显示。<button type="button" className="ml-2 text-primary" onClick={retry}>重新加载</button></div>}><Suspense fallback={<div className="grid h-full place-items-center text-[14px] text-muted-foreground">正在打开 AI 助手…</div>}>
        <Conversation presentation={pageHost ? 'page' : 'mini'} visible={visible} onBusyChange={setBusy} entry={entry} entrySearch={pageHost ? location.search : undefined} />
      </Suspense></ErrorBoundary>
    </TabActiveProvider>, container)}
  </AssistantActivityContext.Provider></Context.Provider>
}

/** 工作区页签只提供承载位置，实际会话由同一个 Provider 持有。 */
export function AssistantPageHost() {
  const context = useAssistantWorkspace()
  const active = useTabActive()
  const host = useRef<HTMLDivElement>(null)
  const register = context?.registerPage
  useLayoutEffect(() => {
    if (!register || !active) return
    register(host.current)
    return () => register(null)
  }, [register, active])
  return <div ref={host} className="min-h-[240px]" data-assistant-page-host />
}
