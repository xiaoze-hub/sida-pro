import { useContext, useEffect, useRef, useState } from 'react'
import type { AnimationItem } from 'lottie-web'
import { useTabActive } from '@sida/base-ui'
import { AssistantActivityContext, loadAssistantAnimation, setAvatarPlayback, useAssistantAvatar, type AssistantAvatarId } from '@/lib/assistant-avatar'

/** 所有助手入口与气泡共用；空闲慢速循环，工作时恢复正常速度。 */
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
  const [loaded, setLoaded] = useState(false)
  const [reduced, setReduced] = useState(() => window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches ?? false)
  const [visible, setVisible] = useState(() => document.visibilityState !== 'hidden')
  const [inView, setInView] = useState(true)
  const active = useTabActive()
  const playing = active && visible && inView && !reduced
  const playingRef = useRef(playing)
  const busyRef = useRef(busy)
  playingRef.current = playing
  busyRef.current = busy

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
    setLoaded(false)
    void loadAssistantAnimation(id).then(({ lottie, data, idleFrame }) => {
      if (disposed || !container.current) return
      instance = lottie.loadAnimation({ container: container.current, renderer: 'svg', animationData: data, autoplay: false, loop: true, rendererSettings: { preserveAspectRatio: 'xMidYMid meet', progressiveLoad: false } })
      animation.current = instance
      idle.current = idleFrame
      const ready = () => {
        if (disposed || !instance) return
        setAvatarPlayback(instance, playingRef.current, idleFrame, busyRef.current, true)
        setLoaded(true)
      }
      instance.addEventListener('DOMLoaded', ready)
      if (instance.isLoaded) ready()
    }).catch(() => { /* 不闪现旧形象；资源不可用时仍保留入口的文字与可访问名称。 */ })
    return () => { disposed = true; instance?.destroy(); animation.current = null }
  }, [id])
  useEffect(() => {
    if (animation.current?.isLoaded) setAvatarPlayback(animation.current, playing, idle.current, busy)
  }, [playing, busy, id])

  return <span className={`relative inline-flex shrink-0 items-center justify-center ${className}`} style={{ width: size, height: size }} role={label ? 'img' : undefined} aria-label={label} aria-hidden={label ? undefined : true} data-assistant-avatar={id} data-animated={loaded && playing ? 'true' : 'false'}>
    <span ref={container} className="assistant-avatar-motion block h-full w-full" style={{ opacity: loaded ? 1 : 0 }} />
  </span>
}
