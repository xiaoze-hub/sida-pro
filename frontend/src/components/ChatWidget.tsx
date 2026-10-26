import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react'
import { ExternalLink, Maximize2, Minimize2, Settings2, X } from 'lucide-react'
import { Button } from '@panwatch/base-ui/components/ui/button'
import { AssistantAvatar } from './AssistantAvatar'
import { useAssistantWorkspace } from './AssistantWorkspace'
import { useCapsulePresent } from '@/lib/copilot/capsule-presence'
import {
  bubbleSide,
  clampToViewport,
  defaultLauncherPoint,
  isDragGesture,
  readLauncherPos,
  writeLauncherPos,
  type Point,
} from '@/lib/copilot/draggable'

const GREETINGS = [
  '从一个标的开始，看看量价与筹码',
  '想复盘今天的走势吗？',
  '一起对照数据，检查技术信号',
  '有疑问？把图表发给我看看',
  '分析会保留数据来源与依据',
]

const ASSISTANT_NAME = '数智参谋'

/** 视口尺寸；无 window（测试/SSR）时给安全兜底。 */
function viewportSize() {
  if (typeof window === 'undefined') return { w: 1280, h: 800 }
  const vv = window.visualViewport
  return { w: vv?.width ?? window.innerWidth, h: vv?.height ?? window.innerHeight }
}

function storage(): Storage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
}

/**
 * 悬浮球 —— 2026-10-26 起可在**全屏任意位置拖动**，位置记在 localStorage。
 *
 * 实现要点：
 *  · Pointer Events 统一鼠标/触摸/触控笔，并 setPointerCapture —— 手指移出球体也不丢事件；
 *  · 位移 ≤4px 判定为点击，否则为拖拽（阈值见 lib/copilot/draggable）；
 *  · 拖拽结束后浏览器仍会补一个 click，必须挡掉，否则"拖完顺手打开了面板"；
 *  · 视口变化（旋转平板 / 软键盘）后重新夹进视口，避免球跑到屏幕外找不回来。
 */
function AssistantLauncher({
  buttonRef,
  busy,
  onClick,
}: {
  buttonRef: RefObject<HTMLButtonElement>
  busy: boolean
  onClick: () => void
}) {
  const [greeting, setGreeting] = useState(() => Math.floor(Math.random() * GREETINGS.length))
  const [point, setPoint] = useState<Point | null>(null)
  const drag = useRef<{ startX: number; startY: number; originX: number; originY: number; moved: boolean } | null>(null)
  // 拖拽结束后浏览器会补一个 click；用它挡掉那一次，避免"拖完顺手打开了面板"。
  const suppressClick = useRef(false)

  // 首帧定位：读回上次位置（夹进当前视口），没有就用右下角默认位。
  useLayoutEffect(() => {
    const el = buttonRef.current
    const size = { w: el?.offsetWidth || 80, h: el?.offsetHeight || 80 }
    const viewport = viewportSize()
    const stored = readLauncherPos(storage())
    setPoint(stored ? clampToViewport(stored, size, viewport) : defaultLauncherPoint(size, viewport))
  }, [buttonRef])

  useEffect(() => {
    const reclamp = () => {
      const el = buttonRef.current
      if (!el) return
      setPoint(previous => previous
        ? clampToViewport(previous, { w: el.offsetWidth, h: el.offsetHeight }, viewportSize())
        : previous)
    }
    window.addEventListener('resize', reclamp)
    window.visualViewport?.addEventListener('resize', reclamp)
    return () => {
      window.removeEventListener('resize', reclamp)
      window.visualViewport?.removeEventListener('resize', reclamp)
    }
  }, [buttonRef])

  useEffect(() => {
    if (busy || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'hidden') return
      setGreeting(previous => (previous + 1 + Math.floor(Math.random() * (GREETINGS.length - 1))) % GREETINGS.length)
    }, 20_000)
    return () => window.clearInterval(timer)
  }, [busy])

  const onPointerDown = useCallback((event: React.PointerEvent<HTMLButtonElement>) => {
    if (event.pointerType === 'mouse' && event.button !== 0) return
    const rect = event.currentTarget.getBoundingClientRect()
    drag.current = { startX: event.clientX, startY: event.clientY, originX: rect.left, originY: rect.top, moved: false }
    event.currentTarget.setPointerCapture?.(event.pointerId)
  }, [])

  const onPointerMove = useCallback((event: React.PointerEvent<HTMLButtonElement>) => {
    const state = drag.current
    if (!state) return
    if (!state.moved && !isDragGesture({ x: state.startX, y: state.startY }, { x: event.clientX, y: event.clientY })) return
    state.moved = true
    const el = event.currentTarget
    setPoint(clampToViewport(
      { x: state.originX + (event.clientX - state.startX), y: state.originY + (event.clientY - state.startY) },
      { w: el.offsetWidth, h: el.offsetHeight },
      viewportSize(),
    ))
  }, [])

  const finishDrag = useCallback((event: React.PointerEvent<HTMLButtonElement>, commit: boolean) => {
    const state = drag.current
    drag.current = null
    event.currentTarget.releasePointerCapture?.(event.pointerId)
    if (!state) return
    if (!state.moved) {
      // 纯点击：交给 button 的 onClick（键盘 Enter/Space 也走这条路，保持单一入口）
      suppressClick.current = false
      if (!commit) suppressClick.current = true
      return
    }
    suppressClick.current = commit
    setPoint(previous => {
      if (previous) writeLauncherPos(storage(), previous)
      return previous
    })
  }, [])

  const side = point ? bubbleSide(point, viewportSize()) : 'left'

  return (
    <button
      ref={buttonRef}
      type="button"
      onClick={event => {
        if (suppressClick.current) {
          suppressClick.current = false
          event.preventDefault()
          event.stopPropagation()
          return
        }
        onClick()
      }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={event => finishDrag(event, true)}
      onPointerCancel={event => finishDrag(event, false)}
      style={point ? { left: point.x, top: point.y } : undefined}
      className={[
        'fixed z-40 flex h-20 w-20 touch-none items-center justify-center rounded-full',
        'transition-transform hover:scale-105 focus-visible:ring-2 focus-visible:ring-ring',
        'active:scale-95 motion-reduce:active:scale-100 md:h-24 md:w-24',
        point ? 'cursor-grab active:cursor-grabbing' : 'bottom-20 right-4 md:bottom-5 md:right-5',
      ].join(' ')}
      title={`拖动可移动 · 点击打开${ASSISTANT_NAME}`}
      aria-label={`打开${ASSISTANT_NAME}`}
      data-testid="assistant-launcher"
      data-draggable="true"
    >
      <span
        aria-hidden="true"
        className={[
          'pointer-events-none absolute w-44 rounded-xl bg-popover px-3 py-2.5 text-left text-[12px] leading-relaxed text-popover-foreground shadow-float md:w-48',
          side === 'left' ? 'right-[calc(100%+4px)]' : 'left-[calc(100%+4px)]',
        ].join(' ')}
        data-testid="assistant-greeting"
      >
        {busy ? '正在分析与整理依据，点击查看进度…' : GREETINGS[greeting]}
        <span className={['absolute top-1/2 h-2 w-2 -translate-y-1/2 rotate-45 bg-popover', side === 'left' ? '-right-1' : '-left-1'].join(' ')} />
      </span>
      <AssistantAvatar size={96} busy={busy} className="max-h-full max-w-full" />
    </button>
  )
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
  // 全屏模式：迷你窗铺满视口（平板/窄屏不用挤在 480px 里）。关窗即退出。
  const [fullscreen, setFullscreen] = useState(false)

  useLayoutEffect(() => {
    if (!register || !open || onPage) return
    register(host.current)
    return () => register(null)
  }, [register, open, onPage])

  useEffect(() => {
    if (!open) {
      setFullscreen(false)
      return
    }
    if (onPage) return
    const updateViewport = () => {
      if (!panel.current) return
      if (fullscreen) {
        // 全屏时交回 CSS 类控制尺寸，清掉内联值，否则会被上面的 maxHeight 压回去。
        panel.current.style.maxHeight = ''
        panel.current.style.bottom = '0px'
        return
      }
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
      if (event.key !== 'Escape' || event.defaultPrevented) return
      // 先退全屏、再关窗 —— 否则按一次 Esc 会把整个会话关掉。
      if (fullscreen) {
        event.preventDefault()
        setFullscreen(false)
        return
      }
      setOpen?.(false)
    }
    window.addEventListener('keydown', escape)
    return () => {
      window.removeEventListener('keydown', escape); observer?.disconnect()
      window.removeEventListener('resize', updateViewport)
      window.visualViewport?.removeEventListener('resize', updateViewport)
      window.visualViewport?.removeEventListener('scroll', updateViewport)
    }
  }, [open, onPage, setOpen, fullscreen])

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

  return (
    <section
      ref={panel}
      role="dialog"
      aria-modal="false"
      aria-label={`${ASSISTANT_NAME}迷你窗口`}
      data-fullscreen={fullscreen ? 'true' : undefined}
      className={[
        'fixed z-40 flex flex-col overflow-hidden bg-popover shadow-float',
        fullscreen
          ? 'inset-0 h-[100dvh] w-full rounded-none'
          : 'bottom-20 right-4 h-[min(680px,calc(100dvh-8rem))] w-[min(480px,calc(100vw-2rem))] rounded-xl md:bottom-5 md:right-5 lg:w-[min(560px,calc(100vw-3rem))]',
      ].join(' ')}
      data-testid="assistant-mini-window"
    >
      <header className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2">
        <AssistantAvatar size={32} />
        <div className="min-w-0 flex-1"><span className="block text-[14px] font-semibold">{ASSISTANT_NAME}</span><span className="text-[12px] text-muted-foreground">{assistant?.busy ? '正在分析与书写…' : '与完整工作台共用同一会话'}</span></div>
        <Button variant="ghost" className="h-11 w-11 md:h-9 md:w-9" aria-label="设置助手形象" title="设置助手形象" onClick={() => assistant?.configure()}><Settings2 className="h-4 w-4" /></Button>
        <Button
          variant="ghost"
          className="h-11 w-11 md:h-9 md:w-9"
          aria-label={fullscreen ? '退出全屏' : '全屏显示'}
          title={fullscreen ? '退出全屏（Esc）' : '全屏显示（平板 / 窄屏更好用）'}
          onClick={() => setFullscreen(previous => !previous)}
        >{fullscreen ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}</Button>
        <Button variant="ghost" className="h-11 w-11 md:h-9 md:w-9" aria-label="在完整工作台打开" title="在完整工作台打开" onClick={() => assistant?.expand()}><ExternalLink className="h-4 w-4" /></Button>
        <Button variant="ghost" className="h-11 w-11 md:h-9 md:w-9" aria-label="关闭迷你助手" title="关闭迷你助手" onClick={() => assistant?.setOpen(false)}><X className="h-4 w-4" /></Button>
      </header>
      <div ref={host} className="min-h-0 flex-1 pb-[env(safe-area-inset-bottom)]" data-assistant-mini-host />
    </section>
  )
}
