import { useContext, useEffect, useRef, useState } from 'react'
import type { AnimationItem } from 'lottie-web'
import { useTabActive } from '@sida/base-ui'
import { AssistantActivityContext, loadAssistantAnimation, setAvatarPlayback, useAssistantAvatar, type AssistantAvatarId } from '@/lib/assistant-avatar'

/**
 * 所有助手入口与气泡共用；空闲慢速循环，工作时恢复正常速度。
 *
 * 2026-10-29：
 *  - **静态球兜底**：Lottie 资源未到/失败时不再是"空白入口"（历史上 404 后该位置什么都没有）。
 *    静态球是纯 CSS 的球体（径向渐变 + 内描边），跟主题 token 走，加载成功后淡出、动效层接管。
 *  - **状态只映射真实状态**：`data-avatar-motion` 只有 playing/paused/static 三种，
 *    playing 由"页签激活 + 可见 + 在视口内 + 未开启减少动效"四个真实条件算出来；
 *    没有"看起来在忙"的装饰态 —— busy 只在真的流式/后台任务时传进来。
 */
export function AssistantAvatar({ variant, busy: working, size = 40, className = '', label }: {
  variant?: AssistantAvatarId; busy?: boolean; size?: number; className?: string; label?: string
}) {
  const selected = useAssistantAvatar()
  const sharedBusy = useContext(AssistantActivityContext)
  const busy = working ?? sharedBusy
  const id = variant ?? selected
  const container = useRef<HTMLSpanElement>(null)
  const animation = useRef<AnimationItem | null>(null)
  const idle = useRef(60)
  /** loading：资源在路上（静态球顶住）｜lottie：动效接管｜static：资源失败，静态球常驻 */
  const [source, setSource] = useState<'loading' | 'lottie' | 'static'>('loading')
  const [reduced, setReduced] = useState(() => window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches ?? false)
  const [visible, setVisible] = useState(() => document.visibilityState !== 'hidden')
  const [inView, setInView] = useState(true)
  const active = useTabActive()
  const playing = active && visible && inView && !reduced
  const playingRef = useRef(playing)
  const busyRef = useRef(busy)
  playingRef.current = playing
  busyRef.current = busy
  const loaded = source === 'lottie'
  /** 只报真实状态：没有动效层就没有 playing（失败=static，未就绪/被暂停=paused）。 */
  const motion = source === 'static' ? 'static' : loaded && playing ? 'playing' : 'paused'

  useEffect(() => {
    const media = window.matchMedia?.('(prefers-reduced-motion: reduce)')
    const updateMotion = () => setReduced(media?.matches ?? false)
    const updateVisibility = () => setVisible(document.visibilityState !== 'hidden')
    updateMotion(); updateVisibility()
    media?.addEventListener?.('change', updateMotion)
    document.addEventListener('visibilitychange', updateVisibility)
    const observer = typeof IntersectionObserver === 'undefined' ? null : new IntersectionObserver(entries => setInView(entries[0]?.isIntersecting ?? false))
    if (container.current) observer?.observe(container.current)
    return () => { media?.removeEventListener?.('change', updateMotion); document.removeEventListener('visibilitychange', updateVisibility); observer?.disconnect() }
  }, [])

  useEffect(() => {
    let disposed = false
    let instance: AnimationItem | null = null
    setSource('loading')
    void loadAssistantAnimation(id).then(({ lottie, data, idleFrame }) => {
      if (disposed || !container.current) return
      instance = lottie.loadAnimation({ container: container.current, renderer: 'svg', animationData: data, autoplay: false, loop: true, rendererSettings: { preserveAspectRatio: 'xMidYMid meet', progressiveLoad: false } })
      animation.current = instance
      idle.current = idleFrame
      const ready = () => {
        if (disposed || !instance) return
        setAvatarPlayback(instance, playingRef.current, idleFrame, busyRef.current, true)
        setSource('lottie')
      }
      instance.addEventListener('DOMLoaded', ready)
      if (instance.isLoaded) ready()
    }).catch(() => {
      // 资源不可用（404/坏 JSON/离线）：静态球常驻，入口仍有形象与可访问名；不重试，避免打满网络。
      if (!disposed) setSource('static')
    })
    return () => { disposed = true; instance?.destroy(); animation.current = null }
  }, [id])
  useEffect(() => {
    if (animation.current?.isLoaded) setAvatarPlayback(animation.current, playing, idle.current, busy)
  }, [playing, busy, id])

  return <span className={`relative inline-flex shrink-0 items-center justify-center ${className}`} style={{ width: size, height: size }} role={label ? 'img' : undefined} aria-label={label} aria-hidden={label ? undefined : true} data-assistant-avatar={id} data-avatar-source={source} data-avatar-motion={motion} data-animated={loaded && playing ? 'true' : 'false'}>
    {/* 静态球：先顶住位置（不闪空白），动效就绪后淡出；也是资源失败时的最终形态。 */}
    <span
      aria-hidden="true"
      data-avatar-static=""
      className="pointer-events-none absolute inset-0 rounded-full transition-opacity duration-300"
      style={{
        opacity: loaded ? 0 : 1,
        background: 'radial-gradient(circle at 34% 26%, hsl(var(--s4)) 0%, hsl(var(--s3)) 42%, hsl(var(--s2)) 100%)',
        boxShadow: 'inset 0 0 0 1px hsl(var(--border-strong)), inset 0 -8px 14px hsl(var(--s2) / 0.6), 0 2px 8px hsl(var(--s0) / 0.16)',
      }}
    />
    <span ref={container} className="assistant-avatar-motion relative block h-full w-full" style={{ opacity: loaded ? 1 : 0 }} />
  </span>
}
