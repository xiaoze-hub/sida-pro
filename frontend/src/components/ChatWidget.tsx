import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react'
import { Maximize2, Settings2, X } from 'lucide-react'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { AssistantAvatar } from './AssistantAvatar'
import { useAssistantWorkspace } from './AssistantWorkspace'
import { useCapsulePresent } from '@/lib/copilot/capsule-presence'

const GREETINGS = [
  '从一个标的开始，看看量价与筹码',
  '想复盘今天的走势吗？',
  '一起对照数据，检查技术信号',
  '有疑问？把图表发给我看看',
  '分析会保留数据来源与依据',
]

function AssistantLauncher({ buttonRef, busy, onClick }: { buttonRef: RefObject<HTMLButtonElement>; busy: boolean; onClick: () => void }) {
  const [greeting, setGreeting] = useState(() => Math.floor(Math.random() * GREETINGS.length))
  useEffect(() => {
    if (busy || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'hidden') return
      setGreeting(previous => (previous + 1 + Math.floor(Math.random() * (GREETINGS.length - 1))) % GREETINGS.length)
    }, 20_000)
    return () => window.clearInterval(timer)
  }, [busy])
  return <button ref={buttonRef} type="button" onClick={onClick} className="fixed bottom-20 right-4 z-40 flex h-20 w-20 items-center justify-center rounded-full transition-transform hover:scale-105 focus-visible:ring-2 focus-visible:ring-ring md:bottom-5 md:right-5 md:h-24 md:w-24" title="打开 AI 分析助手" aria-label="打开 AI 分析助手" data-testid="assistant-launcher">
    <span aria-hidden="true" className="absolute right-[calc(100%+4px)] w-44 rounded-xl bg-popover px-3 py-2.5 text-left text-[12px] leading-relaxed text-popover-foreground shadow-float md:w-48" data-testid="assistant-greeting">
      {busy ? '正在分析与整理依据，点击查看进度…' : GREETINGS[greeting]}
      <span className="absolute -right-1 top-1/2 h-2 w-2 -translate-y-1/2 rotate-45 bg-popover" />
    </span>
    <AssistantAvatar size={96} busy={busy} className="max-h-full max-w-full" />
  </button>
}

/** 完整助手的迷你承载窗；会话、草稿和流均由共享工作区持有。 */
export default function ChatWidget() {
  const assistant = useAssistantWorkspace()
  const open = assistant?.open ?? false
  const register = assistant?.registerMini
  const setOpen = assistant?.setOpen
  const host = useRef<HTMLDivElement>(null)
  const panel = useRef<HTMLElement>(null)
  const launcher = useRef<HTMLButtonElement>(null)
  const wasOpen = useRef(open)
  const capsulePresent = useCapsulePresent()
  const onPage = assistant?.pageActive || window.location.pathname.startsWith('/ai-copilot')
  useLayoutEffect(() => {
    if (!register || !open || onPage) return
    register(host.current)
    return () => register(null)
  }, [register, open, onPage])
  useEffect(() => {
    if (!open || onPage) return
    const updateViewport = () => {
      if (!panel.current) return
      const viewport = window.visualViewport
      const height = viewport?.height ?? window.innerHeight
      const mobile = window.innerWidth < 768
      const navigation = mobile ? document.querySelector('[data-mobile-navigation]')?.getBoundingClientRect().height ?? 56 : 0
      const bottom = mobile ? navigation + 16 : 20
      const top = mobile ? (document.querySelector('[data-mobile-header]')?.getBoundingClientRect().bottom ?? 64) + 12 : 20
      const keyboard = Math.max(0, window.innerHeight - height - (viewport?.offsetTop ?? 0))
      panel.current.style.maxHeight = `${Math.max(180, height - bottom - top)}px`
      panel.current.style.bottom = `${bottom + keyboard}px`
    }
    updateViewport()
    window.addEventListener('resize', updateViewport)
    window.visualViewport?.addEventListener('resize', updateViewport)
    window.visualViewport?.addEventListener('scroll', updateViewport)
    const focusEditor = () => {
      const editor = host.current?.querySelector('textarea')
      if (editor) { editor.focus({ preventScroll: true }); observer?.disconnect() }
    }
    const observer = typeof MutationObserver === 'undefined' ? null : new MutationObserver(focusEditor)
    if (host.current) observer?.observe(host.current, { childList: true, subtree: true })
    focusEditor()
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.defaultPrevented) setOpen?.(false)
    }
    window.addEventListener('keydown', escape)
    return () => {
      window.removeEventListener('keydown', escape); observer?.disconnect()
      window.removeEventListener('resize', updateViewport)
      window.visualViewport?.removeEventListener('resize', updateViewport)
      window.visualViewport?.removeEventListener('scroll', updateViewport)
    }
  }, [open, onPage, setOpen])
  useEffect(() => {
    if (wasOpen.current && !open) launcher.current?.focus({ preventScroll: true })
    wasOpen.current = open
  }, [open])
  if (assistant && !assistant.enabled) return null
  if (onPage) return null
  if (!open) {
    if (capsulePresent) return null
    return <AssistantLauncher buttonRef={launcher} busy={assistant?.busy ?? false} onClick={() => assistant ? assistant.setOpen(true) : window.location.assign('/ai-copilot')} />
  }
  return <section ref={panel} role="dialog" aria-modal="false" aria-label="AI 分析助手迷你窗口" className="fixed bottom-20 right-4 z-40 flex h-[min(680px,calc(100dvh-8rem))] w-[min(480px,calc(100vw-2rem))] flex-col overflow-hidden rounded-xl bg-popover shadow-float md:bottom-5 md:right-5" data-testid="assistant-mini-window">
    <header className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2">
      <AssistantAvatar size={32} />
      <div className="min-w-0 flex-1"><span className="block text-[14px] font-semibold">AI 分析助手</span><span className="text-[12px] text-muted-foreground">{assistant?.busy ? '正在分析与书写…' : '与完整工作台共用同一会话'}</span></div>
      <Button variant="ghost" size="icon" aria-label="设置助手形象" title="设置助手形象" onClick={() => assistant?.configure()}><Settings2 className="h-4 w-4" /></Button>
      <Button variant="ghost" size="icon" aria-label="在完整工作台打开" title="在完整工作台打开" onClick={() => assistant?.expand()}><Maximize2 className="h-4 w-4" /></Button>
      <Button variant="ghost" size="icon" aria-label="关闭迷你助手" onClick={() => assistant?.setOpen(false)}><X className="h-4 w-4" /></Button>
    </header>
    <div ref={host} className="min-h-0 flex-1" data-assistant-mini-host />
  </section>
}
