import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useLocation, useNavigate } from 'react-router-dom'
import { TabActiveProvider, useTabActive } from '@sida/base-ui'
import { AssistantConversationLoader } from '@/components/AssistantConversationLoader'
import { AssistantActivityContext } from '@/lib/assistant-avatar'
import { readStockRouteAnchor } from '@/lib/copilot/route-anchor'

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
  /**
   * 当前路由是不是一张个股/指数工作台（2026-11-18）：停靠面板据此跟随"你在看哪只票"。
   * 没有 entry 时（用户直接点悬浮球打开、或面板开着换股）这是唯一可靠的锚点来源；
   * 路由离开 `/stocks/:symbol` 就变成 null → 面板锚点随之清空。
   */
  const routeAnchor = useMemo(
    () => readStockRouteAnchor(location.pathname, location.search),
    [location.pathname, location.search],
  )
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
    window.addEventListener('sida-open-chat', handleEntry)
    return () => window.removeEventListener('sida-open-chat', handleEntry)
  }, [enabled])
  const expand = useCallback(() => { setOpen(false); navigate('/ai-copilot') }, [navigate])
  const configure = useCallback(() => { setOpen(false); navigate('/settings?tab=settings&section=sec-appearance') }, [navigate])
  const value = useMemo(() => ({ open, setOpen, busy, enabled, pageActive: !!pageHost, registerPage, registerMini, expand, configure }), [open, busy, enabled, pageHost, registerPage, registerMini, expand, configure])
  return <Context.Provider value={value}><AssistantActivityContext.Provider value={busy}>
    {children}
    <div ref={parking} hidden aria-hidden="true" />
    {started && enabled && createPortal(<TabActiveProvider active={visible}>
      <AssistantConversationLoader
        presentation={pageHost ? 'page' : 'mini'}
        visible={visible}
        onBusyChange={setBusy}
        entry={entry}
        entrySearch={pageHost ? location.search : undefined}
        routeAnchor={routeAnchor}
      />
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
