import { useEffect, useRef, useState } from 'react'
import { useTabActive } from '@panwatch/base-ui'
import { loadLotionScene, type AnimationItem, type LotionScene } from '@/lib/lotion'

/**
 * 通用 Lottie 场景容器（2026-10-27）。
 *
 * 规则（与 AssistantAvatar 同一套纪律，保持一致才好维护）：
 *  · `prefers-reduced-motion` → 不播动画，直接停在**末帧**（不是空白，末帧本身就是完整图形）；
 *  · 页面隐藏 / 标签页非活动 / 滚出视口 → 暂停，不留后台 RAF；
 *  · 加载失败 → 渲染等尺寸占位，**绝不因为动效挂掉拖垮整块 UI**；
 *  · 装饰性动效默认 `aria-hidden`，不进无障碍树。
 */
export function LotionScene({
  scene,
  size,
  playing = true,
  color,
  className = '',
  label,
  onComplete,
}: {
  scene: LotionScene
  /** 正方形边长（px）；非正方形传 {w,h}。 */
  size: number | { w: number; h: number }
  playing?: boolean
  /** 覆盖资产里的 token 色，一般传 `var(--primary)` 解析后的实际色值。 */
  color?: string
  className?: string
  /** 有语义时传（如「扫描中」），否则保持装饰性。 */
  label?: string
  onComplete?: () => void
}) {
  const host = useRef<HTMLSpanElement>(null)
  const animation = useRef<AnimationItem | null>(null)
  const [ready, setReady] = useState(false)
  const [failed, setFailed] = useState(false)
  const [reduced, setReduced] = useState(() => typeof window !== 'undefined' && (window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches ?? false))
  const [visible, setVisible] = useState(() => typeof document === 'undefined' || document.visibilityState !== 'hidden')
  const [inView, setInView] = useState(true)
  const active = useTabActive()
  const done = useRef(false)

  const width = typeof size === 'number' ? size : size.w
  const height = typeof size === 'number' ? size : size.h

  useEffect(() => {
    const media = window.matchMedia?.('(prefers-reduced-motion: reduce)')
    const updateMotion = () => setReduced(media?.matches ?? false)
    const updateVisibility = () => setVisible(document.visibilityState !== 'hidden')
    updateMotion()
    updateVisibility()
    media?.addEventListener?.('change', updateMotion)
    document.addEventListener('visibilitychange', updateVisibility)
    const observer = typeof IntersectionObserver === 'undefined' ? null : new IntersectionObserver(entries => setInView(entries[0]?.isIntersecting ?? false))
    if (host.current) observer?.observe(host.current)
    return () => {
      media?.removeEventListener?.('change', updateMotion)
      document.removeEventListener('visibilitychange', updateVisibility)
      observer?.disconnect()
    }
  }, [])

  // 颜色跟随主题：拿容器实际计算色，主题切换时 CSS 变量变了再取一次。
  const [resolvedColor, setResolvedColor] = useState<string | undefined>(color)
  useEffect(() => {
    if (color) {
      setResolvedColor(color)
      return
    }
    const node = host.current
    if (!node) return
    const read = () => setResolvedColor(getComputedStyle(node).color)
    read()
    const observer = typeof MutationObserver === 'undefined' ? null : new MutationObserver(read)
    observer?.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-theme'] })
    return () => observer?.disconnect()
  }, [color])

  useEffect(() => {
    let cancelled = false
    if (!host.current || !resolvedColor) return
    loadLotionScene(scene, resolvedColor)
      .then(({ lottie, data, loop }) => {
        if (cancelled || !host.current) return
        const instance = lottie.loadAnimation({
          container: host.current,
          renderer: 'svg',
          loop,
          autoplay: false,
          animationData: data,
          rendererSettings: { preserveAspectRatio: 'xMidYMid meet', progressiveLoad: false },
        })
        animation.current = instance
        instance.addEventListener('DOMLoaded', () => {
          if (cancelled) return
          setReady(true)
          // 只播一次的场景 + 减少动效：直接停在末帧（末帧是完整图形，不是半截）。
          instance.setSpeed(1)
          if (!loop) {
            instance.goToAndStop(Math.max(0, (instance.totalFrames || 1) - 1), true)
          }
        })
        instance.addEventListener('complete', () => {
          done.current = true
          onComplete?.()
        })
        return instance
      })
      .catch(() => { if (!cancelled) setFailed(true) })
    return () => {
      cancelled = true
      animation.current?.destroy()
      animation.current = null
    }
    // onComplete 每次渲染都可能是新函数，不该重启动画
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scene, resolvedColor])

  const shouldPlay = playing && !reduced && visible && inView && active
  useEffect(() => {
    const instance = animation.current
    if (!instance || !ready) return
    const loop = instance.loop
    if (!loop && done.current) return
    if (shouldPlay) instance.play()
    else instance.pause()
  }, [shouldPlay, ready])

  const style = { width, height }
  if (failed) {
    // 等尺寸占位：动效失败不能造成布局跳动
    return <span aria-hidden="true" className={className} style={style} data-lotion-failed={scene} />
  }

  return (
    <span
      ref={host}
      role={label ? 'img' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      className={`inline-block shrink-0 align-middle ${className}`}
      style={style}
      data-lotion={scene}
      data-lotion-ready={ready ? 'true' : undefined}
    />
  )
}

export default LotionScene
