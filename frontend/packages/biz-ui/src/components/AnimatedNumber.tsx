import { useEffect, useRef, useState } from 'react'

/**
 * 数字滚动(2026-09-05 UI 全量改造 Wave1)。
 * value 变化时 600ms ease-out 滚动到新值; 数字用 font-num 等宽, 不抖动。
 * 尊重 prefers-reduced-motion(直接跳变)。格式化由 format 交给调用方
 * (金额亿/万、涨跌幅符号等口径保持各业务原样, 本组件只管动画)。
 */
export default function AnimatedNumber({
  value,
  format,
  className,
  duration = 600,
}: {
  value: number | null | undefined
  format: (v: number) => string
  className?: string
  duration?: number
}) {
  const [display, setDisplay] = useState<number | null>(value ?? null)
  const rafRef = useRef<number>(0)
  const fromRef = useRef<number>(0)

  useEffect(() => {
    if (value == null || !Number.isFinite(value)) {
      setDisplay(null)
      return
    }
    const from = fromRef.current
    if (from === value) {
      setDisplay(value)
      return
    }
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches || duration <= 0) {
      fromRef.current = value
      setDisplay(value)
      return
    }
    const t0 = performance.now()
    const tick = (t: number) => {
      const p = Math.min(1, (t - t0) / duration)
      const eased = 1 - Math.pow(1 - p, 3)
      const cur = from + (value - from) * eased
      setDisplay(cur)
      if (p < 1) {
        rafRef.current = requestAnimationFrame(tick)
      } else {
        fromRef.current = value
      }
    }
    cancelAnimationFrame(rafRef.current)
    rafRef.current = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(rafRef.current)
  }, [value, duration])

  // 首挂载直接定位, 不从 0 滚(避免满屏数字一起滚的廉价感)
  useEffect(() => {
    if (value != null && Number.isFinite(value)) fromRef.current = value
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (display == null) return <span className={className}>--</span>
  return <span className={className}>{format(display)}</span>
}
