import { useEffect, useRef, useState } from 'react'
import { cn } from '@panwatch/base-ui'

/**
 * 涨跌闪(2026-09-05 UI 全量改造 Wave1)。
 * value 变大闪红 / 变小闪绿 / 首挂载不闪。颜色走 --stock-up/down 令牌(买红卖绿)。
 * 用 key 切换重触发 CSS 动画, 比 setTimeout 稳。
 */
export default function FlashValue({
  value,
  children,
  className,
}: {
  value: number | null | undefined
  children: React.ReactNode
  className?: string
}) {
  const [flash, setFlash] = useState<'up' | 'down' | null>(null)
  const prevRef = useRef<number | null>(null)
  const timerRef = useRef<number>(0)
  const keyRef = useRef(0)

  useEffect(() => {
    const prev = prevRef.current
    prevRef.current = value ?? null
    if (value == null || prev == null || value === prev) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    setFlash(value > prev ? 'up' : 'down')
    keyRef.current += 1
    window.clearTimeout(timerRef.current)
    timerRef.current = window.setTimeout(() => setFlash(null), 650)
    return () => window.clearTimeout(timerRef.current)
  }, [value])

  if (!flash) return <span className={className}>{children}</span>
  return (
    <span
      key={keyRef.current}
      className={cn(
        'sida-flash',
        flash === 'up' ? 'sida-flash-up' : 'sida-flash-down',
        className,
      )}
    >
      {children}
    </span>
  )
}
